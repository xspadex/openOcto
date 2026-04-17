package com.openocto.app

import android.app.Notification
import android.app.PendingIntent
import android.app.Service
import android.content.Intent
import android.os.IBinder
import android.util.Log
import androidx.core.app.NotificationCompat
import org.json.JSONObject
import java.net.Inet4Address
import java.net.NetworkInterface

/**
 * Foreground service that runs the openOcto daemon.
 * Polls Redis for tasks, executes them, and reports results.
 */
class DaemonService : Service() {

    private var relay: RelayClient? = null
    private var taskHandler: TaskHandler? = null
    private var lanServer: LanServer? = null
    private var nearbyTransfer: NearbyTransferManager? = null
    private var pollThread: Thread? = null
    private var heartbeatThread: Thread? = null
    private var running = false
    private var terminalName = ""

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        when (intent?.action) {
            ACTION_STOP -> {
                stopDaemon()
                stopSelf()
                return START_NOT_STICKY
            }
            ACTION_TRANSFER_ACCEPT -> {
                handleTransferResponse(true)
                return START_STICKY
            }
            ACTION_TRANSFER_REJECT -> {
                handleTransferResponse(false)
                return START_STICKY
            }
            else -> {
                startDaemon()
                return START_STICKY
            }
        }
    }

    private fun startDaemon() {
        if (running) return

        val config = OctoConfig(this)
        if (!config.isConfigured) {
            Log.e(TAG, "Not configured")
            stopSelf()
            return
        }

        terminalName = config.terminalName
        relay = RelayClient(config.redisUrl, config.redisToken, config.workspace,
            proxyUrl = config.proxyUrl)
        taskHandler = TaskHandler(this, relay!!, terminalName, config)

        running = true
        isRunning = true

        // MUST call startForeground immediately before any network/blocking ops
        startForeground(NOTIFICATION_ID, buildNotification("Connecting as '$terminalName'..."))

        // Start LAN server
        lanServer = LanServer()
        lanServer?.start()

        // Start BLE advertising for nearby P2P transfer discovery
        try {
            nearbyTransfer = NearbyTransferManager(this).apply {
                setDeviceName(terminalName)
                // Show a confirmation dialog when someone wants to send a file
                onTransferConfirmation = { senderAddress, onResult ->
                    showTransferConfirmation(senderAddress, onResult)
                }
                startAdvertising()
            }
            Log.i(TAG, "BLE nearby advertising started")
        } catch (e: Exception) {
            Log.w(TAG, "BLE advertising failed (non-fatal): ${e.message}")
        }

        val lanIp = getLanIp()
        val tags = config.tags.split(",").map { it.trim() }.filter { it.isNotEmpty() }

        // Register terminal (in background to avoid blocking)
        Thread {
            try {
                relay!!.register(terminalName, tags, mapOf(
                    "cwd" to "/sdcard",
                    "ssh" to "",
                    "shell" to "sh",
                    "platform" to "android",
                    "lan_ip" to lanIp,
                    "lan_port" to 9527
                ))
                relay!!.setMode(terminalName, "wake")
                Log.i(TAG, "Registered: $terminalName (LAN: $lanIp)")
            } catch (e: Exception) {
                Log.e(TAG, "Registration failed: ${e.message}")
            }
        }.start()

        // Start heartbeat thread
        heartbeatThread = Thread {
            while (running) {
                try {
                    relay?.heartbeat(terminalName)
                } catch (e: Exception) {
                    Log.e(TAG, "Heartbeat failed: ${e.message}")
                }
                Thread.sleep(HEARTBEAT_INTERVAL)
            }
        }.apply { isDaemon = true; start() }

        // Start poll thread
        pollThread = Thread { pollLoop() }.apply { isDaemon = true; start() }

        Log.i(TAG, "Daemon started: $terminalName (LAN: $lanIp)")
    }

    private fun stopDaemon() {
        running = false
        isRunning = false
        nearbyTransfer?.stopAdvertising()
        nearbyTransfer?.destroy()
        nearbyTransfer = null
        lanServer?.stop()
        try {
            relay?.unregister(terminalName)
        } catch (_: Exception) {}
        pollThread?.interrupt()
        heartbeatThread?.interrupt()
        Log.i(TAG, "Daemon stopped")
    }

    override fun onDestroy() {
        stopDaemon()
        super.onDestroy()
    }

    private fun pollLoop() {
        var currentInterval = WAKE_POLL_MIN
        var lastMode = "wake"
        var wakeIdleSince = System.currentTimeMillis()
        var lastModeCheck = 0L

        while (running) {
            try {
                val now = System.currentTimeMillis()

                // Check mode every 15s
                if (now - lastModeCheck >= 15_000) {
                    val mode = relay?.getMode(terminalName) ?: "cool"
                    lastModeCheck = now

                    if (mode != lastMode) {
                        if (mode == "wake") {
                            currentInterval = WAKE_POLL_MIN
                            wakeIdleSince = now
                        } else {
                            currentInterval = COOL_POLL
                        }
                        lastMode = mode
                        Log.i(TAG, "Mode: ${mode.uppercase()}")
                    }
                }

                // Check for notifications
                try {
                    val notifications = relay?.popNotifications(terminalName) ?: emptyList()
                    for (n in notifications) {
                        showTaskNotification(
                            n.optString("title", "OpenOcto"),
                            n.optString("body", ""),
                            n.optString("source", "")
                        )
                    }
                } catch (_: Exception) {}

                // Poll for task
                val task = relay?.pollTask(terminalName)
                if (task != null && task.optString("status") == "PENDING") {
                    val taskId = task.optString("id", "?")
                    val taskType = task.optString("type", "shell")
                    Log.i(TAG, "Task $taskId ($taskType)")

                    relay?.updateTask(terminalName, mapOf("status" to "RUNNING"))

                    val (output, exitCode) = taskHandler!!.dispatch(task)

                    // Truncate if needed
                    val finalOutput = if (output.length > MAX_OUTPUT) {
                        "[octo] Output truncated\n" + output.takeLast(MAX_OUTPUT)
                    } else output

                    relay?.completeTask(terminalName, finalOutput, exitCode)

                    val status = if (exitCode == 0) "OK" else "FAILED (exit $exitCode)"
                    Log.i(TAG, "Task $taskId: $status")

                    currentInterval = WAKE_POLL_MIN
                    wakeIdleSince = System.currentTimeMillis()
                    if (lastMode == "cool") {
                        lastMode = "wake"
                        relay?.setMode(terminalName, "wake")
                    }
                } else if (lastMode == "wake") {
                    currentInterval = minOf(
                        (currentInterval * WAKE_BACKOFF).toLong(),
                        WAKE_POLL_MAX
                    )
                    if (now - wakeIdleSince > WAKE_TIMEOUT) {
                        relay?.setMode(terminalName, "cool")
                        currentInterval = COOL_POLL
                        lastMode = "cool"
                        Log.i(TAG, "Auto-cooled after idle")
                    }
                }
            } catch (e: InterruptedException) {
                break
            } catch (e: Exception) {
                Log.e(TAG, "Poll error: ${e.message}")
            }

            try {
                Thread.sleep(currentInterval)
            } catch (_: InterruptedException) {
                break
            }
        }
    }

    // ---- Nearby Transfer Confirmation ----

    companion object {
        const val TAG = "OctoDaemon"
        const val ACTION_START = "com.openocto.START"
        const val ACTION_STOP = "com.openocto.STOP"
        const val ACTION_TRANSFER_ACCEPT = "com.openocto.TRANSFER_ACCEPT"
        const val ACTION_TRANSFER_REJECT = "com.openocto.TRANSFER_REJECT"
        const val NOTIFICATION_ID = 1
        const val TRANSFER_NOTIFICATION_ID = 2
        const val HEARTBEAT_INTERVAL = 30_000L
        const val COOL_POLL = 60_000L
        const val WAKE_POLL_MIN = 1_000L
        const val WAKE_POLL_MAX = 10_000L
        const val WAKE_BACKOFF = 1.5
        const val WAKE_TIMEOUT = 300_000L
        const val MAX_OUTPUT = 200_000

        var isRunning = false
            private set
    }

    private var transferCallback: ((Boolean) -> Unit)? = null

    private fun showTransferConfirmation(senderAddress: String, onResult: (Boolean) -> Unit) {
        transferCallback = onResult

        val acceptIntent = Intent(this, DaemonService::class.java).apply {
            action = ACTION_TRANSFER_ACCEPT
        }
        val rejectIntent = Intent(this, DaemonService::class.java).apply {
            action = ACTION_TRANSFER_REJECT
        }

        val acceptPi = PendingIntent.getService(this, 10, acceptIntent,
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE)
        val rejectPi = PendingIntent.getService(this, 11, rejectIntent,
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE)

        val notification = NotificationCompat.Builder(this, OctoApp.CHANNEL_ID)
            .setContentTitle("Incoming File Transfer")
            .setContentText("Device $senderAddress wants to send you a file")
            .setSmallIcon(android.R.drawable.ic_dialog_info)
            .setPriority(NotificationCompat.PRIORITY_HIGH)
            .setCategory(NotificationCompat.CATEGORY_CALL)
            .setAutoCancel(true)
            .addAction(android.R.drawable.ic_menu_send, "Accept", acceptPi)
            .addAction(android.R.drawable.ic_menu_close_clear_cancel, "Reject", rejectPi)
            .build()

        val nm = getSystemService(NOTIFICATION_SERVICE) as android.app.NotificationManager
        nm.notify(TRANSFER_NOTIFICATION_ID, notification)
    }

    private fun handleTransferResponse(accepted: Boolean) {
        val nm = getSystemService(NOTIFICATION_SERVICE) as android.app.NotificationManager
        nm.cancel(TRANSFER_NOTIFICATION_ID)
        transferCallback?.invoke(accepted)
        transferCallback = null
    }

    private var notificationCounter = 1000

    private fun showTaskNotification(title: String, body: String, source: String) {
        val intent = Intent(this, MainActivity::class.java)
        val pendingIntent = PendingIntent.getActivity(
            this, notificationCounter, intent,
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE
        )

        val displayTitle = if (source.isNotEmpty()) "$title ($source)" else title

        val notification = NotificationCompat.Builder(this, OctoApp.CHANNEL_ID)
            .setContentTitle(displayTitle)
            .setContentText(body.take(200))
            .setStyle(NotificationCompat.BigTextStyle().bigText(body))
            .setSmallIcon(android.R.drawable.ic_dialog_info)
            .setPriority(NotificationCompat.PRIORITY_HIGH)
            .setContentIntent(pendingIntent)
            .setAutoCancel(true)
            .build()

        val nm = getSystemService(NOTIFICATION_SERVICE) as android.app.NotificationManager
        nm.notify(notificationCounter++, notification)
        Log.i(TAG, "Notification: $displayTitle")
    }

    private fun buildNotification(text: String): Notification {
        val intent = Intent(this, MainActivity::class.java)
        val pendingIntent = PendingIntent.getActivity(
            this, 0, intent,
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE
        )

        return NotificationCompat.Builder(this, OctoApp.CHANNEL_ID)
            .setContentTitle(getString(R.string.notification_title))
            .setContentText(text)
            .setSmallIcon(android.R.drawable.ic_dialog_info)
            .setContentIntent(pendingIntent)
            .setOngoing(true)
            .build()
    }

    private fun getLanIp(): String {
        try {
            val interfaces = NetworkInterface.getNetworkInterfaces()
            while (interfaces.hasMoreElements()) {
                val iface = interfaces.nextElement()
                if (iface.isLoopback || !iface.isUp) continue
                val addresses = iface.inetAddresses
                while (addresses.hasMoreElements()) {
                    val addr = addresses.nextElement()
                    if (addr is Inet4Address && !addr.isLoopbackAddress) {
                        return addr.hostAddress ?: ""
                    }
                }
            }
        } catch (_: Exception) {}
        return ""
    }
}
