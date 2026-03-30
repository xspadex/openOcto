package com.openocto.app

import android.annotation.SuppressLint
import android.bluetooth.*
import android.bluetooth.le.*
import android.content.Context
import android.net.wifi.WifiManager
import android.os.Build
import android.os.ParcelUuid
import android.util.Log
import java.io.*
import java.net.InetAddress
import java.net.ServerSocket
import java.net.Socket
import java.nio.ByteBuffer
import java.nio.ByteOrder
import java.security.SecureRandom
import java.util.UUID
import java.util.concurrent.ConcurrentHashMap
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicBoolean
import javax.crypto.Cipher
import javax.crypto.spec.GCMParameterSpec
import javax.crypto.spec.SecretKeySpec

/**
 * Nearby P2P file transfer: BLE discovery + Wi-Fi hotspot + TCP transfer.
 *
 * Protocol flow:
 *   1. BLE: devices advertise/scan with service UUID, exchange names
 *   2. Sender creates local-only hotspot, writes SSID+passphrase+port via BLE GATT
 *   3. Receiver connects to hotspot
 *   4. TCP: OCTO binary protocol with AES-256-GCM encryption
 *
 * Wire format (each frame):
 *   [4B magic "OCTO"] [1B version] [1B type] [8B payload_len] [12B nonce] [payload + 16B GCM tag]
 *   Types: 0x01=file_meta, 0x02=chunk, 0x03=ack, 0x04=done, 0x05=error
 */
class NearbyTransferManager(private val context: Context) {

    companion object {
        private const val TAG = "NearbyTransfer"

        // BLE service UUID for openOcto nearby discovery
        val SERVICE_UUID: UUID = UUID.fromString("0000OCTO-0000-1000-8000-00805F9B34FB"
            .replace("OCTO", "0C70"))
        val CHAR_DEVICE_NAME_UUID: UUID = UUID.fromString("0000OC71-0000-1000-8000-00805F9B34FB"
            .replace("OC71", "0C71"))
        val CHAR_HOTSPOT_INFO_UUID: UUID = UUID.fromString("0000OC72-0000-1000-8000-00805F9B34FB"
            .replace("OC72", "0C72"))
        // Writable: sender writes "PREPARE_RECEIVE" to trigger hotspot + TCP server
        val CHAR_TRIGGER_UUID: UUID = UUID.fromString("0000OC73-0000-1000-8000-00805F9B34FB"
            .replace("OC73", "0C73"))

        private const val MAGIC = "OCTO"
        private const val VERSION: Byte = 0x01
        private const val TYPE_FILE_META: Byte = 0x01
        private const val TYPE_CHUNK: Byte = 0x02
        private const val TYPE_ACK: Byte = 0x03
        private const val TYPE_DONE: Byte = 0x04
        private const val TYPE_ERROR: Byte = 0x05

        private const val CHUNK_SIZE = 1024 * 1024 // 1MB chunks
        private const val HEADER_SIZE = 4 + 1 + 1 + 8 + 12 // magic + ver + type + len + nonce = 26
        private const val GCM_TAG_BITS = 128
        private const val GCM_NONCE_SIZE = 12
        private const val TCP_PORT = 9528

        private const val BLE_SCAN_TIMEOUT = 10_000L
        private const val HOTSPOT_TIMEOUT = 30_000L
        private const val TRANSFER_TIMEOUT = 300_000L
    }

    // ---- State ----

    data class NearbyDevice(
        val name: String,
        val address: String, // BLE MAC
        val rssi: Int = 0,
        val discoveredAt: Long = System.currentTimeMillis()
    )

    interface TransferCallback {
        fun onDeviceFound(device: NearbyDevice)
        fun onProgress(fileName: String, bytesSent: Long, totalBytes: Long)
        fun onComplete(fileName: String, savedPath: String?)
        fun onError(message: String)
    }

    private val discoveredDevices = ConcurrentHashMap<String, NearbyDevice>()
    private var bleAdvertiser: BluetoothLeAdvertiser? = null
    private var bleScanner: BluetoothLeScanner? = null
    private var gattServer: BluetoothGattServer? = null
    private var isAdvertising = AtomicBoolean(false)
    private var isScanning = AtomicBoolean(false)
    private var hotspotReservation: WifiManager.LocalOnlyHotspotReservation? = null
    private var tcpServer: ServerSocket? = null
    private var sessionKey: ByteArray? = null
    private var deviceName: String = Build.MODEL.lowercase().replace(" ", "-").take(20)

    // Hotspot info to share via BLE
    @Volatile private var hotspotSsid: String = ""
    @Volatile private var hotspotPassphrase: String = ""
    @Volatile private var hotspotPort: Int = TCP_PORT

    // Trigger state: "IDLE" → "PREPARING" → "READY" (hotspot + TCP server up)
    @Volatile private var triggerState: String = "IDLE"

    // Security: one-time challenge token. Sender must read this first, then include it
    // in the PREPARE_RECEIVE command as "PREPARE_RECEIVE:<token>".
    // Token rotates after each use to prevent replay attacks.
    @Volatile private var challengeToken: String = generateChallengeToken()

    // User confirmation for incoming transfers.
    // When a BLE trigger arrives, we ask the user to approve before starting hotspot.
    // Set autoAcceptTransfers=true to skip the prompt (e.g. when AI agent initiates).
    var autoAcceptTransfers: Boolean = false

    /**
     * Callback to show a confirmation dialog. Set by Activity/Service.
     * Parameters: senderAddress, onResult(approved: Boolean)
     * If not set, falls back to autoAcceptTransfers.
     */
    var onTransferConfirmation: ((senderAddress: String, onResult: (Boolean) -> Unit) -> Unit)? = null

    // Pending confirmation state
    private val pendingConfirmLatch = java.util.concurrent.atomic.AtomicReference<CountDownLatch?>(null)
    @Volatile private var pendingConfirmResult = false

    private fun generateChallengeToken(): String {
        val bytes = ByteArray(16)
        java.security.SecureRandom().nextBytes(bytes)
        return bytes.joinToString("") { "%02x".format(it) }
    }

    // Callback for received files (set by DaemonService or UI)
    var onFileReceived: ((fileName: String, savedPath: String) -> Unit)? = null

    fun setDeviceName(name: String) { deviceName = name }

    // =====================================================================
    //  1. BLE Discovery
    // =====================================================================

    /**
     * Scan for nearby Octo devices via BLE. Returns list of found devices.
     */
    @SuppressLint("MissingPermission")
    fun scanForDevices(timeoutMs: Long = BLE_SCAN_TIMEOUT): List<NearbyDevice> {
        discoveredDevices.clear()
        val adapter = BluetoothAdapter.getDefaultAdapter()
            ?: return emptyList<NearbyDevice>().also { Log.w(TAG, "No Bluetooth adapter") }
        if (!adapter.isEnabled) return emptyList<NearbyDevice>().also { Log.w(TAG, "Bluetooth off") }

        bleScanner = adapter.bluetoothLeScanner ?: return emptyList()
        val latch = CountDownLatch(1)

        val scanCallback = object : ScanCallback() {
            override fun onScanResult(callbackType: Int, result: ScanResult) {
                val record = result.scanRecord ?: return
                val name = record.serviceData?.entries?.firstOrNull()?.let {
                    String(it.value, Charsets.UTF_8)
                } ?: result.device.name ?: return

                val device = NearbyDevice(
                    name = name,
                    address = result.device.address,
                    rssi = result.rssi
                )
                discoveredDevices[device.address] = device
                Log.i(TAG, "Found: ${device.name} (${device.address}) rssi=${device.rssi}")
            }

            override fun onScanFailed(errorCode: Int) {
                Log.e(TAG, "BLE scan failed: $errorCode")
                latch.countDown()
            }
        }

        val scanFilter = ScanFilter.Builder()
            .setServiceUuid(ParcelUuid(SERVICE_UUID))
            .build()
        val scanSettings = ScanSettings.Builder()
            .setScanMode(ScanSettings.SCAN_MODE_LOW_LATENCY)
            .build()

        isScanning.set(true)
        bleScanner?.startScan(listOf(scanFilter), scanSettings, scanCallback)
        Log.i(TAG, "BLE scan started (${timeoutMs}ms)")

        latch.await(timeoutMs, TimeUnit.MILLISECONDS)

        bleScanner?.stopScan(scanCallback)
        isScanning.set(false)
        Log.i(TAG, "BLE scan done, found ${discoveredDevices.size} devices")

        return discoveredDevices.values.toList()
    }

    /**
     * Start advertising this device via BLE so others can discover it.
     */
    @SuppressLint("MissingPermission")
    fun startAdvertising() {
        val adapter = BluetoothAdapter.getDefaultAdapter() ?: return
        if (!adapter.isEnabled) return
        bleAdvertiser = adapter.bluetoothLeAdvertiser ?: return

        val settings = AdvertiseSettings.Builder()
            .setAdvertiseMode(AdvertiseSettings.ADVERTISE_MODE_LOW_LATENCY)
            .setTxPowerLevel(AdvertiseSettings.ADVERTISE_TX_POWER_HIGH)
            .setConnectable(true)
            .setTimeout(0)
            .build()

        val nameBytes = deviceName.toByteArray(Charsets.UTF_8).take(20).toByteArray()
        val data = AdvertiseData.Builder()
            .setIncludeDeviceName(false)
            .addServiceUuid(ParcelUuid(SERVICE_UUID))
            .addServiceData(ParcelUuid(SERVICE_UUID), nameBytes)
            .build()

        bleAdvertiser?.startAdvertising(settings, data, object : AdvertiseCallback() {
            override fun onStartSuccess(settingsInEffect: AdvertiseSettings?) {
                isAdvertising.set(true)
                Log.i(TAG, "BLE advertising started as '$deviceName'")
            }

            override fun onStartFailure(errorCode: Int) {
                Log.e(TAG, "BLE advertise failed: $errorCode")
            }
        })

        // Start GATT server to share hotspot info
        startGattServer(adapter)
    }

    @SuppressLint("MissingPermission")
    fun stopAdvertising() {
        if (isAdvertising.getAndSet(false)) {
            bleAdvertiser?.stopAdvertising(object : AdvertiseCallback() {})
            gattServer?.close()
            gattServer = null
            Log.i(TAG, "BLE advertising stopped")
        }
    }

    @SuppressLint("MissingPermission")
    private fun startGattServer(adapter: BluetoothAdapter) {
        val manager = context.getSystemService(Context.BLUETOOTH_SERVICE) as BluetoothManager

        val service = BluetoothGattService(SERVICE_UUID, BluetoothGattService.SERVICE_TYPE_PRIMARY)

        val nameChar = BluetoothGattCharacteristic(
            CHAR_DEVICE_NAME_UUID,
            BluetoothGattCharacteristic.PROPERTY_READ,
            BluetoothGattCharacteristic.PERMISSION_READ
        )
        nameChar.value = deviceName.toByteArray(Charsets.UTF_8)
        service.addCharacteristic(nameChar)

        val hotspotChar = BluetoothGattCharacteristic(
            CHAR_HOTSPOT_INFO_UUID,
            BluetoothGattCharacteristic.PROPERTY_READ,
            BluetoothGattCharacteristic.PERMISSION_READ
        )
        service.addCharacteristic(hotspotChar)

        // Writable trigger: sender writes here to make this device start hotspot + TCP server
        val triggerChar = BluetoothGattCharacteristic(
            CHAR_TRIGGER_UUID,
            BluetoothGattCharacteristic.PROPERTY_WRITE or BluetoothGattCharacteristic.PROPERTY_READ,
            BluetoothGattCharacteristic.PERMISSION_WRITE or BluetoothGattCharacteristic.PERMISSION_READ
        )
        service.addCharacteristic(triggerChar)

        gattServer = manager.openGattServer(context, object : BluetoothGattServerCallback() {
            override fun onCharacteristicReadRequest(
                device: BluetoothDevice, requestId: Int, offset: Int,
                characteristic: BluetoothGattCharacteristic
            ) {
                val value = when (characteristic.uuid) {
                    CHAR_DEVICE_NAME_UUID -> deviceName.toByteArray(Charsets.UTF_8)
                    CHAR_HOTSPOT_INFO_UUID -> "$hotspotSsid\n$hotspotPassphrase\n$hotspotPort"
                        .toByteArray(Charsets.UTF_8)
                    // Returns "STATE:token" — sender reads this to get the challenge token
                    CHAR_TRIGGER_UUID -> "$triggerState:$challengeToken".toByteArray(Charsets.UTF_8)
                    else -> ByteArray(0)
                }
                gattServer?.sendResponse(device, requestId, BluetoothGatt.GATT_SUCCESS, offset,
                    if (offset < value.size) value.copyOfRange(offset, value.size) else ByteArray(0))
            }

            override fun onCharacteristicWriteRequest(
                device: BluetoothDevice, requestId: Int,
                characteristic: BluetoothGattCharacteristic,
                preparedWrite: Boolean, responseNeeded: Boolean,
                offset: Int, value: ByteArray
            ) {
                if (characteristic.uuid == CHAR_TRIGGER_UUID) {
                    val cmd = String(value, Charsets.UTF_8).trim()
                    Log.i(TAG, "BLE trigger received: '$cmd' from ${device.address}")
                    // Expected format: "PREPARE_RECEIVE:<token>"
                    val parts = cmd.split(":", limit = 2)
                    if (parts.size == 2 && parts[0] == "PREPARE_RECEIVE"
                        && parts[1] == challengeToken) {
                        // Token valid — rotate it so it can't be replayed
                        challengeToken = generateChallengeToken()
                        val senderAddr = device.address
                        Thread { confirmAndReceive(senderAddr) }.start()
                    } else {
                        Log.w(TAG, "BLE trigger rejected: invalid command or token")
                    }
                }
                if (responseNeeded) {
                    gattServer?.sendResponse(device, requestId, BluetoothGatt.GATT_SUCCESS, offset, value)
                }
            }
        })

        gattServer?.addService(service)
    }

    // =====================================================================
    //  2. Wi-Fi Hotspot
    // =====================================================================

    /**
     * Create a local-only Wi-Fi hotspot (no internet sharing, no data cost).
     * Returns (ssid, passphrase) or null on failure.
     */
    @SuppressLint("MissingPermission")
    fun createHotspot(): Pair<String, String>? {
        val wifiManager = context.applicationContext.getSystemService(Context.WIFI_SERVICE) as WifiManager
        val latch = CountDownLatch(1)
        var result: Pair<String, String>? = null

        try {
            wifiManager.startLocalOnlyHotspot(object : WifiManager.LocalOnlyHotspotCallback() {
                override fun onStarted(reservation: WifiManager.LocalOnlyHotspotReservation) {
                    hotspotReservation = reservation
                    val config = reservation.wifiConfiguration
                        ?: reservation.softApConfiguration?.let { sac ->
                            android.net.wifi.WifiConfiguration().apply {
                                SSID = sac.ssid
                                preSharedKey = sac.passphrase
                            }
                        }
                    if (config != null) {
                        hotspotSsid = config.SSID ?: ""
                        hotspotPassphrase = config.preSharedKey ?: ""
                        result = hotspotSsid to hotspotPassphrase
                        Log.i(TAG, "Hotspot created: SSID=$hotspotSsid")
                    }
                    latch.countDown()
                }

                override fun onFailed(reason: Int) {
                    Log.e(TAG, "Hotspot failed: reason=$reason")
                    latch.countDown()
                }
            }, null)
        } catch (e: Exception) {
            Log.e(TAG, "Hotspot error: ${e.message}")
            return null
        }

        latch.await(HOTSPOT_TIMEOUT, TimeUnit.MILLISECONDS)
        return result
    }

    fun stopHotspot() {
        try {
            hotspotReservation?.close()
            hotspotReservation = null
            hotspotSsid = ""
            hotspotPassphrase = ""
            Log.i(TAG, "Hotspot stopped")
        } catch (e: Exception) {
            Log.e(TAG, "Hotspot stop error: ${e.message}")
        }
    }

    // =====================================================================
    //  2b. Auto-triggered receive: hotspot + TCP server (called via BLE trigger)
    // =====================================================================

    /**
     * Called when a remote sender writes "PREPARE_RECEIVE" to our BLE GATT trigger.
     * Creates hotspot, starts TCP server to receive incoming file, updates GATT state.
     */
    /**
     * Ask user for confirmation, then start receive.
     * Skips prompt if autoAcceptTransfers is true (AI agent mode).
     */
    private fun confirmAndReceive(senderAddress: String) {
        if (autoAcceptTransfers) {
            Log.i(TAG, "Auto-accepting transfer from $senderAddress (agent mode)")
            prepareToReceive()
            return
        }

        val callback = onTransferConfirmation
        if (callback == null) {
            // No UI callback registered — reject by default for safety
            Log.w(TAG, "No confirmation callback, rejecting transfer from $senderAddress")
            triggerState = "IDLE"
            return
        }

        triggerState = "CONFIRMING"
        Log.i(TAG, "Asking user to confirm transfer from $senderAddress")

        val latch = CountDownLatch(1)
        var approved = false

        callback(senderAddress) { result ->
            approved = result
            latch.countDown()
        }

        // Wait up to 30s for user response
        latch.await(30, TimeUnit.SECONDS)

        if (approved) {
            Log.i(TAG, "User approved transfer from $senderAddress")
            prepareToReceive()
        } else {
            Log.i(TAG, "User rejected transfer from $senderAddress")
            triggerState = "IDLE"
        }
    }

    private fun prepareToReceive() {
        triggerState = "PREPARING"
        Log.i(TAG, "Preparing to receive (triggered via BLE)...")

        // Create hotspot
        val hotspot = createHotspot()
        if (hotspot == null) {
            Log.e(TAG, "Failed to create hotspot for receive")
            triggerState = "ERROR"
            return
        }

        hotspotPort = TCP_PORT
        triggerState = "READY"
        Log.i(TAG, "Ready to receive: SSID=${hotspot.first}, port=$hotspotPort")

        // Start TCP server to accept incoming file
        Thread {
            var server: ServerSocket? = null
            try {
                server = ServerSocket(TCP_PORT)
                server.soTimeout = TRANSFER_TIMEOUT.toInt()
                tcpServer = server
                Log.i(TAG, "TCP server listening on $TCP_PORT for incoming file...")

                val socket = server.accept()
                socket.soTimeout = TRANSFER_TIMEOUT.toInt()
                Log.i(TAG, "Sender connected: ${socket.inetAddress}")

                val inp = BufferedInputStream(socket.getInputStream())
                val out = BufferedOutputStream(socket.getOutputStream())

                // Receive session key
                val key = ByteArray(32)
                var read = 0
                while (read < 32) {
                    val n = inp.read(key, read, 32 - read)
                    if (n < 0) throw IOException("Connection closed during key exchange")
                    read += n
                }
                sessionKey = key

                // Read file metadata
                val (metaType, metaData) = readFrame(inp)
                if (metaType != TYPE_FILE_META) {
                    Log.e(TAG, "Expected file_meta, got $metaType")
                    socket.close()
                    return@Thread
                }

                val metaJson = org.json.JSONObject(String(metaData, Charsets.UTF_8))
                val fileName = metaJson.getString("name")
                val fileSize = metaJson.getLong("size")
                Log.i(TAG, "Receiving: $fileName ($fileSize bytes)")

                writeFrame(out, TYPE_ACK, "OK".toByteArray())

                // Receive chunks
                val saveDir = java.io.File("/sdcard/Download")
                saveDir.mkdirs()
                val saveFile = java.io.File(saveDir, fileName)
                val fos = FileOutputStream(saveFile)
                var received = 0L

                while (true) {
                    val (frameType, frameData) = readFrame(inp)
                    when (frameType) {
                        TYPE_CHUNK -> {
                            fos.write(frameData)
                            received += frameData.size
                        }
                        TYPE_DONE -> break
                        TYPE_ERROR -> {
                            fos.close(); saveFile.delete()
                            Log.e(TAG, "Sender error: ${String(frameData)}")
                            socket.close(); return@Thread
                        }
                    }
                }
                fos.close()
                writeFrame(out, TYPE_ACK, "DONE".toByteArray())
                socket.close()

                Log.i(TAG, "Received: ${saveFile.absolutePath} ($received bytes)")
                onFileReceived?.invoke(fileName, saveFile.absolutePath)

            } catch (e: Exception) {
                Log.e(TAG, "Receive error: ${e.message}", e)
            } finally {
                server?.close()
                tcpServer = null
                // Cleanup after transfer
                stopHotspot()
                triggerState = "IDLE"
            }
        }.start()
    }

    // =====================================================================
    //  3. TCP Transfer — Sender (Server)
    // =====================================================================

    /**
     * Send a file to a connected peer. Call after hotspot is up and peer has connected.
     * This starts a TCP server, waits for one connection, then streams the file.
     */
    fun sendFile(
        filePath: String,
        callback: TransferCallback
    ) {
        sessionKey = generateSessionKey()
        hotspotPort = TCP_PORT

        Thread {
            var server: ServerSocket? = null
            try {
                val file = File(filePath)
                if (!file.exists()) {
                    callback.onError("File not found: $filePath")
                    return@Thread
                }

                server = ServerSocket(TCP_PORT)
                server.soTimeout = TRANSFER_TIMEOUT.toInt()
                tcpServer = server
                Log.i(TAG, "TCP server listening on port $TCP_PORT, waiting for receiver...")

                val socket = server.accept()
                socket.soTimeout = TRANSFER_TIMEOUT.toInt()
                Log.i(TAG, "Receiver connected: ${socket.inetAddress}")

                val out = BufferedOutputStream(socket.getOutputStream())
                val inp = BufferedInputStream(socket.getInputStream())

                // Exchange session key (simplified: send key in plaintext over local hotspot)
                // In production, use ECDH key exchange. For local hotspot this is acceptable
                // since only the connected peer can reach this port.
                out.write(sessionKey!!)
                out.flush()

                // Send file metadata
                val meta = """{"name":"${file.name}","size":${file.length()},"chunk_size":$CHUNK_SIZE}"""
                writeFrame(out, TYPE_FILE_META, meta.toByteArray(Charsets.UTF_8))

                // Wait for ACK
                val ack = readFrame(inp)
                if (ack.first != TYPE_ACK) {
                    callback.onError("Expected ACK, got type ${ack.first}")
                    socket.close()
                    return@Thread
                }

                // Stream file chunks
                val fis = FileInputStream(file)
                val totalSize = file.length()
                var sent = 0L
                val buf = ByteArray(CHUNK_SIZE)

                while (true) {
                    val read = fis.read(buf)
                    if (read <= 0) break
                    val chunk = if (read == buf.size) buf else buf.copyOf(read)
                    writeFrame(out, TYPE_CHUNK, chunk)
                    sent += read
                    callback.onProgress(file.name, sent, totalSize)
                }
                fis.close()

                // Send done
                writeFrame(out, TYPE_DONE, ByteArray(0))

                // Wait final ACK
                val finalAck = readFrame(inp)
                socket.close()

                callback.onComplete(file.name, null)
                Log.i(TAG, "Send complete: ${file.name} ($sent bytes)")

            } catch (e: Exception) {
                Log.e(TAG, "Send error: ${e.message}", e)
                callback.onError("Send failed: ${e.message}")
            } finally {
                server?.close()
                tcpServer = null
            }
        }.start()
    }

    // =====================================================================
    //  4. TCP Transfer — Receiver (Client)
    // =====================================================================

    /**
     * Receive a file from a sender. Connect to the sender's TCP server.
     */
    fun receiveFile(
        host: String,
        port: Int = TCP_PORT,
        saveDir: String = "/sdcard/Download",
        callback: TransferCallback
    ) {
        Thread {
            try {
                val socket = Socket(host, port)
                socket.soTimeout = TRANSFER_TIMEOUT.toInt()
                Log.i(TAG, "Connected to sender: $host:$port")

                val inp = BufferedInputStream(socket.getInputStream())
                val out = BufferedOutputStream(socket.getOutputStream())

                // Receive session key
                sessionKey = ByteArray(32)
                var read = 0
                while (read < 32) {
                    val n = inp.read(sessionKey!!, read, 32 - read)
                    if (n < 0) throw IOException("Connection closed during key exchange")
                    read += n
                }

                // Read file metadata
                val (metaType, metaData) = readFrame(inp)
                if (metaType != TYPE_FILE_META) {
                    callback.onError("Expected file_meta, got type $metaType")
                    socket.close()
                    return@Thread
                }

                val metaJson = org.json.JSONObject(String(metaData, Charsets.UTF_8))
                val fileName = metaJson.getString("name")
                val fileSize = metaJson.getLong("size")
                Log.i(TAG, "Receiving: $fileName ($fileSize bytes)")

                // Send ACK
                writeFrame(out, TYPE_ACK, "OK".toByteArray())

                // Receive chunks
                val saveFile = File(saveDir, fileName)
                saveFile.parentFile?.mkdirs()
                val fos = FileOutputStream(saveFile)
                var received = 0L

                while (true) {
                    val (frameType, frameData) = readFrame(inp)
                    when (frameType) {
                        TYPE_CHUNK -> {
                            fos.write(frameData)
                            received += frameData.size
                            callback.onProgress(fileName, received, fileSize)
                        }
                        TYPE_DONE -> break
                        TYPE_ERROR -> {
                            fos.close()
                            saveFile.delete()
                            callback.onError("Sender error: ${String(frameData)}")
                            socket.close()
                            return@Thread
                        }
                        else -> Log.w(TAG, "Unexpected frame type: $frameType")
                    }
                }
                fos.close()

                // Send final ACK
                writeFrame(out, TYPE_ACK, "DONE".toByteArray())
                socket.close()

                callback.onComplete(fileName, saveFile.absolutePath)
                Log.i(TAG, "Receive complete: ${saveFile.absolutePath} ($received bytes)")

            } catch (e: Exception) {
                Log.e(TAG, "Receive error: ${e.message}", e)
                callback.onError("Receive failed: ${e.message}")
            }
        }.start()
    }

    // =====================================================================
    //  5. Wire Protocol — AES-256-GCM encrypted frames
    // =====================================================================

    private fun writeFrame(out: OutputStream, type: Byte, plaintext: ByteArray) {
        val key = sessionKey ?: throw IllegalStateException("No session key")
        val nonce = ByteArray(GCM_NONCE_SIZE).also { SecureRandom().nextBytes(it) }

        val cipher = Cipher.getInstance("AES/GCM/NoPadding")
        cipher.init(Cipher.ENCRYPT_MODE, SecretKeySpec(key, "AES"), GCMParameterSpec(GCM_TAG_BITS, nonce))
        val ciphertext = cipher.doFinal(plaintext)

        val header = ByteBuffer.allocate(HEADER_SIZE).order(ByteOrder.LITTLE_ENDIAN)
        header.put(MAGIC.toByteArray(Charsets.US_ASCII))
        header.put(VERSION)
        header.put(type)
        header.putLong(ciphertext.size.toLong())
        header.put(nonce)

        out.write(header.array())
        out.write(ciphertext)
        out.flush()
    }

    private fun readFrame(inp: InputStream): Pair<Byte, ByteArray> {
        val key = sessionKey ?: throw IllegalStateException("No session key")
        val header = readExact(inp, HEADER_SIZE)
        val buf = ByteBuffer.wrap(header).order(ByteOrder.LITTLE_ENDIAN)

        val magic = ByteArray(4)
        buf.get(magic)
        if (String(magic) != MAGIC) throw IOException("Invalid magic: ${String(magic)}")

        val ver = buf.get()
        val type = buf.get()
        val payloadLen = buf.getLong()
        val nonce = ByteArray(GCM_NONCE_SIZE)
        buf.get(nonce)

        if (payloadLen > 100 * 1024 * 1024) throw IOException("Frame too large: $payloadLen")

        val ciphertext = readExact(inp, payloadLen.toInt())

        val cipher = Cipher.getInstance("AES/GCM/NoPadding")
        cipher.init(Cipher.DECRYPT_MODE, SecretKeySpec(key, "AES"), GCMParameterSpec(GCM_TAG_BITS, nonce))
        val plaintext = cipher.doFinal(ciphertext)

        return type to plaintext
    }

    private fun readExact(inp: InputStream, len: Int): ByteArray {
        val buf = ByteArray(len)
        var offset = 0
        while (offset < len) {
            val n = inp.read(buf, offset, len - offset)
            if (n < 0) throw IOException("Unexpected EOF, expected $len bytes, got $offset")
            offset += n
        }
        return buf
    }

    private fun generateSessionKey(): ByteArray {
        val key = ByteArray(32)
        SecureRandom().nextBytes(key)
        return key
    }

    // =====================================================================
    //  6. High-level API for Agent tool
    // =====================================================================

    /**
     * Scan for nearby Octo devices. Returns human-readable result.
     */
    fun toolScan(timeoutMs: Long = BLE_SCAN_TIMEOUT): String {
        val devices = scanForDevices(timeoutMs)
        if (devices.isEmpty()) return "No nearby Octo devices found."
        val sb = StringBuilder("Found ${devices.size} nearby device(s):\n")
        for (d in devices) {
            sb.appendLine("  - ${d.name} (signal: ${d.rssi}dBm)")
        }
        return sb.toString()
    }

    /**
     * Send a file to a nearby device. Full flow:
     * 1. Start advertising + hotspot
     * 2. Wait for receiver to connect
     * 3. Transfer file
     * 4. Cleanup
     */
    fun toolSend(filePath: String): String {
        val file = File(filePath)
        if (!file.exists()) return "Error: file not found: $filePath"
        if (!file.isFile) return "Error: not a file: $filePath"

        val latch = CountDownLatch(1)
        var resultMsg = ""

        // Create hotspot
        val hotspot = createHotspot()
            ?: return "Error: failed to create Wi-Fi hotspot. Check Wi-Fi is enabled."

        Log.i(TAG, "Hotspot ready: SSID=${hotspot.first}")

        // Start advertising so receiver can find us
        startAdvertising()

        // Start TCP server and send
        sendFile(filePath, object : TransferCallback {
            override fun onDeviceFound(device: NearbyDevice) {}
            override fun onProgress(fileName: String, bytesSent: Long, totalBytes: Long) {
                val pct = if (totalBytes > 0) (bytesSent * 100 / totalBytes) else 0
                Log.i(TAG, "Sending $fileName: $pct%")
            }

            override fun onComplete(fileName: String, savedPath: String?) {
                resultMsg = "Sent '$fileName' (${file.length() / 1024}KB) successfully."
                cleanup()
                latch.countDown()
            }

            override fun onError(message: String) {
                resultMsg = "Error: $message"
                cleanup()
                latch.countDown()
            }

            private fun cleanup() {
                stopAdvertising()
                stopHotspot()
            }
        })

        // Wait for transfer to complete (with timeout)
        val completed = latch.await(TRANSFER_TIMEOUT, TimeUnit.MILLISECONDS)
        if (!completed) {
            stopAdvertising()
            stopHotspot()
            return "Error: transfer timed out"
        }

        return resultMsg
    }

    /**
     * Read hotspot info from a sender's BLE GATT, connect to hotspot, receive file.
     */
    @SuppressLint("MissingPermission")
    fun toolReceive(senderAddress: String, saveDir: String = "/sdcard/Download"): String {
        val adapter = BluetoothAdapter.getDefaultAdapter()
            ?: return "Error: no Bluetooth adapter"

        val latch = CountDownLatch(1)
        var hotspotInfo = ""
        var resultMsg = ""

        // Connect to sender's GATT to get hotspot info
        val device = adapter.getRemoteDevice(senderAddress)
        val gattLatch = CountDownLatch(1)

        device.connectGatt(context, false, object : BluetoothGattCallback() {
            override fun onConnectionStateChange(gatt: BluetoothGatt, status: Int, newState: Int) {
                if (newState == BluetoothGatt.STATE_CONNECTED) {
                    gatt.discoverServices()
                } else if (newState == BluetoothGatt.STATE_DISCONNECTED) {
                    gattLatch.countDown()
                }
            }

            override fun onServicesDiscovered(gatt: BluetoothGatt, status: Int) {
                val service = gatt.getService(SERVICE_UUID)
                val char = service?.getCharacteristic(CHAR_HOTSPOT_INFO_UUID)
                if (char != null) {
                    gatt.readCharacteristic(char)
                } else {
                    gattLatch.countDown()
                }
            }

            override fun onCharacteristicRead(
                gatt: BluetoothGatt, characteristic: BluetoothGattCharacteristic,
                value: ByteArray, status: Int
            ) {
                if (status == BluetoothGatt.GATT_SUCCESS) {
                    hotspotInfo = String(value, Charsets.UTF_8)
                }
                gatt.disconnect()
                gatt.close()
                gattLatch.countDown()
            }
        })

        gattLatch.await(15, TimeUnit.SECONDS)

        if (hotspotInfo.isEmpty()) return "Error: could not read hotspot info from sender"

        val parts = hotspotInfo.split("\n")
        if (parts.size < 3) return "Error: invalid hotspot info"
        val ssid = parts[0]
        val passphrase = parts[1]
        val port = parts[2].toIntOrNull() ?: TCP_PORT

        Log.i(TAG, "Got hotspot info: SSID=$ssid, port=$port")

        // Connect to hotspot using WifiNetworkSpecifier
        val connected = connectToHotspot(ssid, passphrase)
        if (!connected) return "Error: failed to connect to sender's hotspot"

        // Find sender's IP (gateway of the hotspot network)
        Thread.sleep(2000) // wait for DHCP
        val gatewayIp = getGatewayIp()
        if (gatewayIp.isEmpty()) return "Error: could not determine sender's IP"

        Log.i(TAG, "Connected to hotspot, sender IP: $gatewayIp")

        // Receive file
        receiveFile(gatewayIp, port, saveDir, object : TransferCallback {
            override fun onDeviceFound(device: NearbyDevice) {}
            override fun onProgress(fileName: String, bytesSent: Long, totalBytes: Long) {
                val pct = if (totalBytes > 0) (bytesSent * 100 / totalBytes) else 0
                Log.i(TAG, "Receiving $fileName: $pct%")
            }

            override fun onComplete(fileName: String, savedPath: String?) {
                resultMsg = "Received '$fileName' → $savedPath"
                latch.countDown()
            }

            override fun onError(message: String) {
                resultMsg = "Error: $message"
                latch.countDown()
            }
        })

        latch.await(TRANSFER_TIMEOUT, TimeUnit.MILLISECONDS)
        disconnectFromHotspot()
        return resultMsg.ifEmpty { "Error: transfer timed out" }
    }

    // =====================================================================
    //  7. Wi-Fi connection helpers
    // =====================================================================

    @SuppressLint("MissingPermission")
    private fun connectToHotspot(ssid: String, passphrase: String): Boolean {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
            val specifier = android.net.wifi.WifiNetworkSpecifier.Builder()
                .setSsid(ssid)
                .setWpa2Passphrase(passphrase)
                .build()
            val request = android.net.NetworkRequest.Builder()
                .addTransportType(android.net.NetworkCapabilities.TRANSPORT_WIFI)
                .setNetworkSpecifier(specifier)
                .build()
            val cm = context.getSystemService(Context.CONNECTIVITY_SERVICE) as android.net.ConnectivityManager
            val latch = CountDownLatch(1)
            var success = false

            cm.requestNetwork(request, object : android.net.ConnectivityManager.NetworkCallback() {
                override fun onAvailable(network: android.net.Network) {
                    cm.bindProcessToNetwork(network)
                    success = true
                    latch.countDown()
                }

                override fun onUnavailable() {
                    latch.countDown()
                }
            })

            latch.await(HOTSPOT_TIMEOUT, TimeUnit.MILLISECONDS)
            return success
        } else {
            // Pre-Q: use WifiManager directly
            val wifiManager = context.applicationContext.getSystemService(Context.WIFI_SERVICE) as WifiManager
            @Suppress("DEPRECATION")
            val conf = android.net.wifi.WifiConfiguration().apply {
                SSID = "\"$ssid\""
                preSharedKey = "\"$passphrase\""
            }
            @Suppress("DEPRECATION")
            val netId = wifiManager.addNetwork(conf)
            if (netId < 0) return false
            @Suppress("DEPRECATION")
            wifiManager.enableNetwork(netId, true)
            @Suppress("DEPRECATION")
            wifiManager.reconnect()
            Thread.sleep(5000) // wait for connection
            return true
        }
    }

    private fun disconnectFromHotspot() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
            val cm = context.getSystemService(Context.CONNECTIVITY_SERVICE) as android.net.ConnectivityManager
            cm.bindProcessToNetwork(null)
        }
    }

    private fun getGatewayIp(): String {
        try {
            val wifiManager = context.applicationContext.getSystemService(Context.WIFI_SERVICE) as WifiManager
            @Suppress("DEPRECATION")
            val dhcp = wifiManager.dhcpInfo
            val gateway = dhcp.gateway
            if (gateway != 0) {
                return InetAddress.getByAddress(
                    ByteBuffer.allocate(4).order(ByteOrder.LITTLE_ENDIAN).putInt(gateway).array()
                ).hostAddress ?: ""
            }
        } catch (e: Exception) {
            Log.e(TAG, "Gateway IP error: ${e.message}")
        }
        return ""
    }

    // =====================================================================
    //  Cleanup
    // =====================================================================

    fun destroy() {
        stopAdvertising()
        stopHotspot()
        tcpServer?.close()
    }
}
