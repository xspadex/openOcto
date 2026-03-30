package com.openocto.app

import android.Manifest
import android.content.ClipData
import android.content.ClipboardManager
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.location.LocationManager
import android.media.AudioManager
import android.net.Uri
import android.os.BatteryManager
import android.os.Build
import android.os.Environment
import android.os.VibrationEffect
import android.os.Vibrator
import android.os.VibratorManager
import android.provider.ContactsContract
import android.speech.tts.TextToSpeech
import android.telephony.SmsManager
import android.util.Base64
import android.util.Log
import org.json.JSONObject
import java.io.*
import java.net.HttpURLConnection
import java.net.URL
import java.nio.file.Files
import java.nio.file.Paths
import java.util.Locale
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit
import java.util.regex.Pattern

/**
 * Handles task execution on Android.
 * Implements: shell, cat, edit, glob, grep, transfer_download, clipboard,
 *             send_sms, read_sms, make_call, set_alarm, get_location,
 *             read_contacts, device_info, send_notification, tts, vibrate,
 *             set_volume, open_app
 */
class TaskHandler(
    private val context: Context,
    private val relay: RelayClient,
    private val terminalName: String,
    private val config: OctoConfig? = null
) {
    private val TAG = "OctoTaskHandler"
    private val storageRoot = Environment.getExternalStorageDirectory().canonicalPath // /storage/emulated/0
    private var cwd: String = Environment.getExternalStorageDirectory().absolutePath

    // ---- File Access Control ----

    private fun getAllowedPaths(): List<String> {
        val level = config?.fileAccessLevel ?: "documents"
        return when (level) {
            "photos" -> listOf(
                java.io.File(storageRoot, "DCIM").canonicalPath,
                java.io.File(storageRoot, "Pictures").canonicalPath,
            )
            "documents" -> listOf(
                java.io.File(storageRoot, "DCIM").canonicalPath,
                java.io.File(storageRoot, "Pictures").canonicalPath,
                java.io.File(storageRoot, "Documents").canonicalPath,
                java.io.File(storageRoot, "Download").canonicalPath,
                java.io.File(storageRoot, ".octo").canonicalPath,
            )
            else -> listOf(storageRoot) // "full"
        }
    }

    private val blockedPaths = listOf("/Android/data", "/Android/obb")

    private fun isPathAllowed(path: String): Boolean {
        val resolved = java.io.File(path).canonicalPath
        // Block other apps' private data at any level
        if (blockedPaths.any { resolved.contains(it) }) return false
        val allowed = getAllowedPaths()
        return allowed.any { resolved.startsWith(it) }
    }

    private fun checkPath(path: String): Pair<String, Int>? {
        if (!isPathAllowed(path)) {
            val level = config?.fileAccessLevel ?: "documents"
            return "Access denied: $path (current level: $level). Change file access level in Permissions." to 1
        }
        return null
    }

    private fun isShellAllowed(): Boolean {
        val level = config?.fileAccessLevel ?: "documents"
        return level == "full"
    }

    private fun hasPermission(permission: String): Boolean {
        return androidx.core.content.ContextCompat.checkSelfPermission(
            context, permission
        ) == PackageManager.PERMISSION_GRANTED
    }

    private fun requirePermission(permission: String, label: String): Pair<String, Int>? {
        if (!hasPermission(permission)) {
            return "Permission required: $label. Please grant '$label' permission in Settings > Permissions and retry." to 1
        }
        return null
    }

    fun dispatch(task: JSONObject): Pair<String, Int> {
        val type = task.optString("type", "shell")
        return try {
            when (type) {
                "shell" -> {
                    if (!isShellAllowed()) return "Shell is disabled in '${config?.fileAccessLevel ?: "documents"}' access level. Set file access to 'full' to enable shell." to 1
                    execShell(task)
                }
                "cat" -> { checkPath(resolvePath(task.getString("path")))?.let { return it }; execCat(task) }
                "edit" -> { checkPath(resolvePath(task.getString("path")))?.let { return it }; execEdit(task) }
                "glob" -> { checkPath(resolvePath(task.optString("path", cwd)))?.let { return it }; execGlob(task) }
                "grep" -> { checkPath(resolvePath(task.optString("path", cwd)))?.let { return it }; execGrep(task) }
                "transfer_download" -> execTransferDownload(task)
                "inbox_receive" -> execInboxReceive(task)
                "inbox_list" -> execInboxList(task)
                "clipboard_write" -> execClipboardWrite(task)
                "clipboard_read" -> execClipboardRead(task)
                "send_sms" -> requirePermission(Manifest.permission.SEND_SMS, "SMS") ?: execSendSms(task)
                "read_sms" -> requirePermission(Manifest.permission.READ_SMS, "Read SMS") ?: execReadSms(task)
                "make_call" -> requirePermission(Manifest.permission.CALL_PHONE, "Phone") ?: execMakeCall(task)
                "set_alarm" -> execSetAlarm(task)
                "get_location" -> requirePermission(Manifest.permission.ACCESS_FINE_LOCATION, "Location") ?: execGetLocation(task)
                "read_contacts" -> requirePermission(Manifest.permission.READ_CONTACTS, "Contacts") ?: execReadContacts(task)
                "device_info" -> execDeviceInfo(task)
                "send_notification" -> execSendNotification(task)
                "tts" -> execTts(task)
                "vibrate" -> execVibrate(task)
                "set_volume" -> execSetVolume(task)
                "open_app" -> execOpenApp(task)
                "nearby_scan" -> execNearbyScan(task)
                "nearby_send" -> execNearbySend(task)
                "nearby_receive" -> execNearbyReceive(task)
                else -> "Unknown task type: $type" to 1
            }
        } catch (e: Exception) {
            Log.e(TAG, "Task error: ${e.message}", e)
            "[octo] Internal error: ${e.message}" to 1
        }
    }

    private fun resolvePath(path: String): String {
        return if (path.startsWith("/")) path
        else "$cwd/$path"
    }

    // ---- Shell ----

    private fun execShell(task: JSONObject): Pair<String, Int> {
        val command = task.getString("command")
        val timeout = task.optLong("timeout", 3600) * 1000

        val pb = ProcessBuilder("sh", "-c", "cd ${shellQuote(cwd)} 2>/dev/null || cd /sdcard; $command; echo __OCTO_CWD__; pwd")
        pb.redirectErrorStream(true)
        val proc = pb.start()

        val output = StringBuilder()
        val reader = BufferedReader(InputStreamReader(proc.inputStream))
        val startTime = System.currentTimeMillis()
        var lastPush = 0L

        try {
            var line: String?
            while (reader.readLine().also { line = it } != null) {
                output.appendLine(line)

                // Stream output every 2 seconds
                val now = System.currentTimeMillis()
                if (now - lastPush >= 2000) {
                    try {
                        relay.updateTask(terminalName, mapOf(
                            "output" to output.toString(),
                            "status" to "RUNNING"
                        ))
                        // Check for kill
                        val remote = relay.pollTask(terminalName)
                        if (remote?.optString("status") == "KILL") {
                            proc.destroyForcibly()
                            output.append("\n[octo] Process killed by user.\n")
                            break
                        }
                    } catch (_: Exception) {}
                    lastPush = now
                }

                // Timeout check
                if (now - startTime > timeout) {
                    proc.destroyForcibly()
                    output.append("\n[octo] Command timed out after ${timeout/1000}s.\n")
                    break
                }
            }
        } finally {
            reader.close()
        }

        proc.waitFor()

        // Extract cwd from output
        val lines = output.toString().trimEnd().split("\n")
        val cwdMarkerIdx = lines.indexOfLast { it.trim() == "__OCTO_CWD__" }
        val finalOutput: String
        if (cwdMarkerIdx >= 0 && cwdMarkerIdx < lines.size - 1) {
            val newCwd = lines[cwdMarkerIdx + 1].trim()
            if (File(newCwd).isDirectory) cwd = newCwd
            finalOutput = lines.subList(0, cwdMarkerIdx).joinToString("\n")
        } else {
            finalOutput = output.toString()
        }

        return finalOutput to (proc.exitValue())
    }

    // ---- File Operations ----

    private fun execCat(task: JSONObject): Pair<String, Int> {
        val path = resolvePath(task.getString("path"))
        val offset = task.optInt("offset", 0)
        val limit = task.optInt("limit", 2000)

        val file = File(path)
        if (!file.exists()) return "File not found: $path" to 1

        val lines = file.readLines()
        val total = lines.size
        val selected = lines.drop(offset).take(limit)
        val sb = StringBuilder()
        selected.forEachIndexed { i, line ->
            sb.appendLine("%6d\t%s".format(offset + i + 1, line))
        }
        if (total > offset + limit) {
            sb.append("\n... (${total - offset - limit} more lines, $total total)")
        }
        return sb.toString() to 0
    }

    private fun execEdit(task: JSONObject): Pair<String, Int> {
        val path = resolvePath(task.getString("path"))
        val old = task.getString("old")
        val new = task.getString("new")
        val replaceAll = task.optBoolean("replace_all", false)

        val file = File(path)
        if (!file.exists()) return "File not found: $path" to 1

        val content = file.readText()
        val count = content.split(old).size - 1

        if (count == 0) return "old_string not found in $path" to 1
        if (count > 1 && !replaceAll) return "old_string matches $count times. Use --replace-all." to 1

        val newContent = if (replaceAll) content.replace(old, new)
                         else content.replaceFirst(old, new)
        file.writeText(newContent)

        val replaced = if (replaceAll) count else 1
        return "Replaced $replaced occurrence(s) in $path" to 0
    }

    private fun execGlob(task: JSONObject): Pair<String, Int> {
        val pattern = task.getString("pattern")
        val basePath = resolvePath(task.optString("path", cwd))
        val baseDir = File(basePath)

        if (!baseDir.exists()) return "Directory not found: $basePath" to 1

        // Simple glob matching using regex
        val regex = globToRegex(pattern)
        val results = mutableListOf<File>()

        baseDir.walkTopDown()
            .filter { it.isFile && regex.matches(it.name) }
            .sortedByDescending { it.lastModified() }
            .take(500)
            .forEach { results.add(it) }

        if (results.isEmpty()) return "No matches." to 0
        return results.joinToString("\n") { it.absolutePath } to 0
    }

    private fun execGrep(task: JSONObject): Pair<String, Int> {
        val pattern = task.getString("pattern")
        val basePath = resolvePath(task.optString("path", cwd))
        val fileGlob = task.optString("glob", "*")
        val baseDir = File(basePath)

        if (!baseDir.exists()) return "Directory not found: $basePath" to 1

        val regex = try { Pattern.compile(pattern) } catch (e: Exception) {
            return "Invalid regex: ${e.message}" to 1
        }
        val fileRegex = globToRegex(fileGlob)
        val results = mutableListOf<String>()

        baseDir.walkTopDown()
            .filter { it.isFile && fileRegex.matches(it.name) }
            .forEach { file ->
                if (results.size >= 1000) return@forEach
                try {
                    file.readLines().forEachIndexed { idx, line ->
                        if (results.size < 1000 && regex.matcher(line).find()) {
                            results.add("${file.absolutePath}:${idx + 1}:$line")
                        }
                    }
                } catch (_: Exception) {}
            }

        if (results.isEmpty()) return "No matches." to 0
        val output = results.joinToString("\n")
        return if (results.size >= 1000) "$output\n... (truncated at 1000 matches)" to 0
               else output to 0
    }

    // ---- Transfer ----

    private fun execTransferDownload(task: JSONObject): Pair<String, Int> {
        val url = task.optString("url", "")
        val redisKey = task.optString("redis_key", "")
        val dest = task.optString("dest", "")

        if (dest.isEmpty()) return "Missing dest path" to 1

        val destFile = File(dest)
        destFile.parentFile?.mkdirs()

        if (url.isNotEmpty()) {
            try {
                val conn = URL(url).openConnection() as HttpURLConnection
                conn.connectTimeout = 30000
                conn.readTimeout = 300000
                conn.inputStream.use { input ->
                    FileOutputStream(destFile).use { output ->
                        input.copyTo(output, 65536)
                    }
                }
                val size = destFile.length()
                return "Downloaded to $dest ($size bytes)" to 0
            } catch (e: Exception) {
                return "Download failed: ${e.message}" to 1
            }
        }

        if (redisKey.isNotEmpty()) {
            try {
                val raw = relay.request("GET", redisKey) ?: return "Transfer key not found" to 1
                val data = Base64.decode(raw, Base64.DEFAULT)
                destFile.writeBytes(data)
                relay.request("DEL", redisKey)
                return "Downloaded to $dest (${data.size} bytes)" to 0
            } catch (e: Exception) {
                return "Redis download failed: ${e.message}" to 1
            }
        }

        return "No url or redis_key provided" to 1
    }

    // ---- Inbox ----

    private fun inboxDir(): File {
        val dir = File(Environment.getExternalStorageDirectory(), ".octo/inbox")
        dir.mkdirs()
        return dir
    }

    private fun manifestFile(): File = File(inboxDir(), ".manifest.json")

    private fun loadManifest(): MutableList<JSONObject> {
        val file = manifestFile()
        if (!file.exists()) return mutableListOf()
        return try {
            val arr = org.json.JSONArray(file.readText())
            (0 until arr.length()).map { arr.getJSONObject(it) }.toMutableList()
        } catch (e: Exception) { mutableListOf() }
    }

    private fun saveManifest(entries: List<JSONObject>) {
        val arr = org.json.JSONArray()
        entries.forEach { arr.put(it) }
        manifestFile().writeText(arr.toString(2))
    }

    private fun execInboxReceive(task: JSONObject): Pair<String, Int> {
        val filename = task.optString("filename", "")
        val sender = task.optString("sender", "unknown")
        val note = task.optString("note", "")
        val url = task.optString("url", "")
        val redisKey = task.optString("redis_key", "")

        if (filename.isEmpty()) return "Missing filename" to 1

        val inbox = inboxDir()
        var actualName = filename
        var dest = File(inbox, actualName)
        if (dest.exists()) {
            val dot = filename.lastIndexOf('.')
            actualName = if (dot > 0)
                "${filename.substring(0, dot)}_${System.currentTimeMillis() / 1000}${filename.substring(dot)}"
            else "${filename}_${System.currentTimeMillis() / 1000}"
            dest = File(inbox, actualName)
        }

        if (url.isNotEmpty()) {
            try {
                val conn = URL(url).openConnection() as HttpURLConnection
                conn.connectTimeout = 30000
                conn.readTimeout = 300000
                conn.inputStream.use { input -> FileOutputStream(dest).use { output -> input.copyTo(output, 65536) } }
            } catch (e: Exception) { return "Download failed: ${e.message}" to 1 }
        } else if (redisKey.isNotEmpty()) {
            try {
                val raw = relay.request("GET", redisKey) ?: return "Transfer key not found" to 1
                dest.writeBytes(Base64.decode(raw, Base64.DEFAULT))
                relay.request("DEL", redisKey)
            } catch (e: Exception) { return "Redis download failed: ${e.message}" to 1 }
        } else {
            return "No url or redis_key provided" to 1
        }

        val size = dest.length()
        val manifest = loadManifest()
        manifest.add(0, JSONObject().apply {
            put("filename", actualName)
            put("sender", sender)
            put("note", note)
            put("size", size)
            put("received_at", System.currentTimeMillis() / 1000)
        })
        // Keep max 100, clean >7 days
        val cutoff = System.currentTimeMillis() / 1000 - 7 * 86400
        val cleaned = manifest.take(100).filter { entry ->
            if (entry.optLong("received_at", 0) < cutoff) {
                File(inbox, entry.optString("filename", "")).delete()
                false
            } else true
        }
        saveManifest(cleaned)

        val result = "Received: $actualName ($size bytes) from $sender" +
            if (note.isNotEmpty()) "\nNote: $note" else ""
        return result to 0
    }

    private fun execInboxList(task: JSONObject): Pair<String, Int> {
        val manifest = loadManifest()
        if (manifest.isEmpty()) return "Inbox is empty." to 0
        val now = System.currentTimeMillis() / 1000
        val lines = manifest.map { entry ->
            val age = now - entry.optLong("received_at", 0)
            val ago = when {
                age < 60 -> "${age}s ago"
                age < 3600 -> "${age / 60}m ago"
                age < 86400 -> "${age / 3600}h ago"
                else -> "${age / 86400}d ago"
            }
            val sizeKb = entry.optLong("size", 0) / 1024.0
            val line = "${entry.optString("filename")}  ${"%.1f".format(sizeKb)}KB  from ${entry.optString("sender", "?")}  $ago"
            if (entry.optString("note", "").isNotEmpty()) "$line\n  Note: ${entry.optString("note")}" else line
        }
        return lines.joinToString("\n") to 0
    }

    // ---- Clipboard ----

    private fun execClipboardWrite(task: JSONObject): Pair<String, Int> {
        val text = task.optString("text", "")
        if (text.isEmpty()) return "No text provided" to 1

        try {
            val handler = android.os.Handler(context.mainLooper)
            var result = ""
            val latch = java.util.concurrent.CountDownLatch(1)
            handler.post {
                try {
                    val clipboard = context.getSystemService(Context.CLIPBOARD_SERVICE) as ClipboardManager
                    clipboard.setPrimaryClip(ClipData.newPlainText("octo", text))
                    result = "Clipboard updated"
                } catch (e: Exception) {
                    result = "Clipboard write failed: ${e.message}"
                }
                latch.countDown()
            }
            latch.await(5, java.util.concurrent.TimeUnit.SECONDS)
            return result to if (result.startsWith("Clipboard updated")) 0 else 1
        } catch (e: Exception) {
            return "Clipboard error: ${e.message}" to 1
        }
    }

    private fun execClipboardRead(task: JSONObject): Pair<String, Int> {
        try {
            val handler = android.os.Handler(context.mainLooper)
            var result = ""
            var code = 0
            val latch = java.util.concurrent.CountDownLatch(1)
            handler.post {
                try {
                    val clipboard = context.getSystemService(Context.CLIPBOARD_SERVICE) as ClipboardManager
                    result = clipboard.primaryClip?.getItemAt(0)?.text?.toString() ?: ""
                } catch (e: Exception) {
                    result = "Clipboard read failed: ${e.message}"
                    code = 1
                }
                latch.countDown()
            }
            latch.await(5, java.util.concurrent.TimeUnit.SECONDS)
            return result to code
        } catch (e: Exception) {
            return "Clipboard error: ${e.message}" to 1
        }
    }

    // ---- SMS ----

    private fun execSendSms(task: JSONObject): Pair<String, Int> {
        val to = task.optString("to", "")
        val message = task.optString("message", "")
        if (to.isEmpty() || message.isEmpty()) return "Missing 'to' or 'message'" to 1
        return try {
            val smsManager = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S)
                context.getSystemService(SmsManager::class.java)
            else @Suppress("DEPRECATION") SmsManager.getDefault()
            val parts = smsManager.divideMessage(message)
            smsManager.sendMultipartTextMessage(to, null, parts, null, null)
            "SMS sent to $to (${parts.size} part(s))" to 0
        } catch (e: Exception) {
            "SMS failed: ${e.message}" to 1
        }
    }

    private fun execReadSms(task: JSONObject): Pair<String, Int> {
        val limit = task.optInt("limit", 10)
        val filter = task.optString("filter", "") // "inbox", "sent", or ""
        val uri = when (filter) {
            "sent" -> Uri.parse("content://sms/sent")
            "inbox" -> Uri.parse("content://sms/inbox")
            else -> Uri.parse("content://sms")
        }
        return try {
            val cursor = context.contentResolver.query(
                uri, arrayOf("address", "body", "date", "type"),
                null, null, "date DESC"
            ) ?: return "Cannot read SMS" to 1
            val results = mutableListOf<String>()
            cursor.use {
                while (it.moveToNext() && results.size < limit) {
                    val addr = it.getString(0) ?: "?"
                    val body = it.getString(1) ?: ""
                    val date = it.getLong(2)
                    val type = if (it.getInt(3) == 1) "recv" else "sent"
                    val time = java.text.SimpleDateFormat("MM-dd HH:mm", Locale.getDefault())
                        .format(java.util.Date(date))
                    results.add("[$time] $type $addr: $body")
                }
            }
            if (results.isEmpty()) "No SMS found." to 0
            else results.joinToString("\n") to 0
        } catch (e: Exception) {
            "Read SMS failed: ${e.message}" to 1
        }
    }

    // ---- Phone Call ----

    private fun execMakeCall(task: JSONObject): Pair<String, Int> {
        val number = task.optString("number", "")
        if (number.isEmpty()) return "Missing 'number'" to 1
        return try {
            val intent = Intent(Intent.ACTION_CALL, Uri.parse("tel:$number")).apply {
                addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
            }
            context.startActivity(intent)
            "Calling $number" to 0
        } catch (e: Exception) {
            "Call failed: ${e.message}" to 1
        }
    }

    // ---- Alarm ----

    private fun execSetAlarm(task: JSONObject): Pair<String, Int> {
        val hour = task.opt("hour")?.toString()?.toIntOrNull() ?: -1
        val minute = task.opt("minute")?.toString()?.toIntOrNull() ?: -1
        val message = task.optString("message", "openOcto alarm")
        val delayMin = task.opt("delay")?.toString()?.toIntOrNull() ?: -1

        if (hour < 0 && delayMin < 0) return "Missing 'hour'+'minute' or 'delay'" to 1

        val delayMs = if (delayMin > 0) {
            delayMin * 60_000L
        } else {
            val cal = java.util.Calendar.getInstance().apply {
                set(java.util.Calendar.HOUR_OF_DAY, hour)
                set(java.util.Calendar.MINUTE, if (minute >= 0) minute else 0)
                set(java.util.Calendar.SECOND, 0)
                set(java.util.Calendar.MILLISECOND, 0)
                if (timeInMillis <= System.currentTimeMillis()) {
                    add(java.util.Calendar.DAY_OF_YEAR, 1)
                }
            }
            cal.timeInMillis - System.currentTimeMillis()
        }

        // In-process timer — runs inside the foreground service, immune to battery optimization
        val handler = android.os.Handler(android.os.Looper.getMainLooper())
        handler.postDelayed({
            fireAlarm(context, message)
        }, delayMs)

        val triggerTime = System.currentTimeMillis() + delayMs
        val sdf = java.text.SimpleDateFormat("HH:mm", java.util.Locale.getDefault())
        val timeStr = sdf.format(java.util.Date(triggerTime))
        val deltaMin = (delayMs / 60000).toInt()
        return "Alarm set for $timeStr ($message) — ${deltaMin}min from now" to 0
    }

    companion object {
        private var mediaPlayer: android.media.MediaPlayer? = null

        fun fireAlarmStop() {
            try { mediaPlayer?.stop(); mediaPlayer?.release() } catch (_: Exception) {}
            mediaPlayer = null
        }

        fun fireAlarm(context: Context, message: String) {
            // Vibrate
            try {
                if (android.os.Build.VERSION.SDK_INT >= android.os.Build.VERSION_CODES.S) {
                    val vm = context.getSystemService(android.content.Context.VIBRATOR_MANAGER_SERVICE) as android.os.VibratorManager
                    vm.defaultVibrator.vibrate(
                        android.os.VibrationEffect.createWaveform(longArrayOf(0, 500, 300, 500, 300, 500), -1)
                    )
                } else {
                    @Suppress("DEPRECATION")
                    val v = context.getSystemService(android.content.Context.VIBRATOR_SERVICE) as android.os.Vibrator
                    v.vibrate(android.os.VibrationEffect.createWaveform(longArrayOf(0, 500, 300, 500, 300, 500), -1))
                }
            } catch (_: Exception) {}

            // Play alarm sound
            try {
                val alarmUri = android.media.RingtoneManager.getDefaultUri(android.media.RingtoneManager.TYPE_ALARM)
                    ?: android.media.RingtoneManager.getDefaultUri(android.media.RingtoneManager.TYPE_NOTIFICATION)
                mediaPlayer?.release()
                mediaPlayer = android.media.MediaPlayer().apply {
                    setDataSource(context, alarmUri)
                    setAudioAttributes(
                        android.media.AudioAttributes.Builder()
                            .setUsage(android.media.AudioAttributes.USAGE_ALARM)
                            .build()
                    )
                    isLooping = false
                    prepare()
                    start()
                    // Auto stop after 30s
                    android.os.Handler(android.os.Looper.getMainLooper()).postDelayed({
                        try { stop(); release() } catch (_: Exception) {}
                        mediaPlayer = null
                    }, 30_000)
                }
            } catch (_: Exception) {}

            // Show notification
            try {
                val dismissIntent = Intent(context, AlarmReceiver::class.java).apply {
                    action = AlarmReceiver.ACTION_DISMISS
                }
                val dismissPi = android.app.PendingIntent.getBroadcast(
                    context, 1, dismissIntent,
                    android.app.PendingIntent.FLAG_UPDATE_CURRENT or android.app.PendingIntent.FLAG_IMMUTABLE
                )
                val notification = androidx.core.app.NotificationCompat.Builder(context, OctoApp.ALARM_CHANNEL_ID)
                    .setContentTitle("Alarm")
                    .setContentText(message)
                    .setSmallIcon(android.R.drawable.ic_lock_idle_alarm)
                    .setPriority(androidx.core.app.NotificationCompat.PRIORITY_HIGH)
                    .setCategory(androidx.core.app.NotificationCompat.CATEGORY_ALARM)
                    .setAutoCancel(true)
                    .addAction(android.R.drawable.ic_delete, "Dismiss", dismissPi)
                    .setSound(null)
                    .build()
                val nm = context.getSystemService(android.content.Context.NOTIFICATION_SERVICE) as android.app.NotificationManager
                nm.notify(9999, notification)
            } catch (_: Exception) {}
        }
    }

    // ---- Location ----

    private fun execGetLocation(task: JSONObject): Pair<String, Int> {
        return try {
            val lm = context.getSystemService(Context.LOCATION_SERVICE) as LocationManager
            // Try last known from any provider
            val providers = listOf(
                LocationManager.FUSED_PROVIDER,
                LocationManager.GPS_PROVIDER,
                LocationManager.NETWORK_PROVIDER
            )
            for (provider in providers) {
                try {
                    @Suppress("MissingPermission")
                    val loc = lm.getLastKnownLocation(provider)
                    if (loc != null) {
                        val age = (System.currentTimeMillis() - loc.time) / 1000
                        return ("lat=${loc.latitude}, lon=${loc.longitude}, " +
                                "accuracy=${loc.accuracy}m, age=${age}s, provider=${loc.provider}") to 0
                    }
                } catch (_: Exception) {}
            }
            "No location available. Ensure location permission is granted and GPS is on." to 1
        } catch (e: Exception) {
            "Location error: ${e.message}" to 1
        }
    }

    // ---- Contacts ----

    private fun execReadContacts(task: JSONObject): Pair<String, Int> {
        val limit = task.optInt("limit", 20)
        val search = task.optString("search", "")
        return try {
            val selection = if (search.isNotEmpty())
                "${ContactsContract.CommonDataKinds.Phone.DISPLAY_NAME} LIKE ?" else null
            val selectionArgs = if (search.isNotEmpty()) arrayOf("%$search%") else null

            val cursor = context.contentResolver.query(
                ContactsContract.CommonDataKinds.Phone.CONTENT_URI,
                arrayOf(
                    ContactsContract.CommonDataKinds.Phone.DISPLAY_NAME,
                    ContactsContract.CommonDataKinds.Phone.NUMBER
                ),
                selection, selectionArgs,
                "${ContactsContract.CommonDataKinds.Phone.DISPLAY_NAME} ASC"
            ) ?: return "Cannot read contacts" to 1

            val results = mutableListOf<String>()
            cursor.use {
                while (it.moveToNext() && results.size < limit) {
                    val name = it.getString(0) ?: "?"
                    val number = it.getString(1) ?: "?"
                    results.add("$name: $number")
                }
            }
            if (results.isEmpty()) "No contacts found." to 0
            else results.joinToString("\n") to 0
        } catch (e: Exception) {
            "Read contacts failed: ${e.message}" to 1
        }
    }

    // ---- Device Info ----

    private fun execDeviceInfo(task: JSONObject): Pair<String, Int> {
        val bm = context.getSystemService(Context.BATTERY_SERVICE) as BatteryManager
        val battery = bm.getIntProperty(BatteryManager.BATTERY_PROPERTY_CAPACITY)
        val charging = bm.isCharging

        val am = context.getSystemService(Context.AUDIO_SERVICE) as AudioManager
        val volume = am.getStreamVolume(AudioManager.STREAM_MUSIC)
        val maxVol = am.getStreamMaxVolume(AudioManager.STREAM_MUSIC)

        val runtime = Runtime.getRuntime()
        val freeMem = runtime.freeMemory() / 1024 / 1024
        val totalMem = runtime.totalMemory() / 1024 / 1024

        val storage = Environment.getExternalStorageDirectory()
        val freeStorage = storage.freeSpace / 1024 / 1024
        val totalStorage = storage.totalSpace / 1024 / 1024

        val info = """
            |device: ${Build.MANUFACTURER} ${Build.MODEL}
            |android: ${Build.VERSION.RELEASE} (API ${Build.VERSION.SDK_INT})
            |battery: ${battery}%${if (charging) " (charging)" else ""}
            |volume: $volume/$maxVol
            |memory: ${freeMem}MB free / ${totalMem}MB
            |storage: ${freeStorage}MB free / ${totalStorage}MB
        """.trimMargin()
        return info to 0
    }

    // ---- Notification ----

    private fun execSendNotification(task: JSONObject): Pair<String, Int> {
        val title = task.optString("title", "openOcto")
        val message = task.optString("message", "")
        if (message.isEmpty()) return "Missing 'message'" to 1
        return try {
            val notifId = (System.currentTimeMillis() % 100000).toInt() + 1000
            val notification = android.app.Notification.Builder(context, OctoApp.CHANNEL_ID)
                .setContentTitle(title)
                .setContentText(message)
                .setSmallIcon(android.R.drawable.ic_dialog_info)
                .setAutoCancel(true)
                .build()
            val nm = context.getSystemService(Context.NOTIFICATION_SERVICE) as android.app.NotificationManager
            nm.notify(notifId, notification)
            "Notification sent: $title" to 0
        } catch (e: Exception) {
            "Notification failed: ${e.message}" to 1
        }
    }

    // ---- TTS ----

    private fun execTts(task: JSONObject): Pair<String, Int> {
        val text = task.optString("text", "")
        if (text.isEmpty()) return "Missing 'text'" to 1
        val lang = task.optString("lang", "")
        val latch = CountDownLatch(1)
        var result = "TTS playback started" to 0

        var tts: TextToSpeech? = null
        tts = TextToSpeech(context) { status ->
            if (status == TextToSpeech.SUCCESS) {
                if (lang.isNotEmpty()) {
                    tts?.setLanguage(Locale.forLanguageTag(lang))
                }
                tts?.speak(text, TextToSpeech.QUEUE_FLUSH, null, "octo_tts")
                // Wait for speech to finish
                Thread {
                    Thread.sleep(500) // let it start
                    while (tts?.isSpeaking == true) Thread.sleep(200)
                    tts?.shutdown()
                    latch.countDown()
                }.start()
            } else {
                result = "TTS init failed" to 1
                latch.countDown()
            }
        }
        latch.await(30, TimeUnit.SECONDS)
        return result
    }

    // ---- Vibrate ----

    private fun execVibrate(task: JSONObject): Pair<String, Int> {
        val duration = task.opt("duration")?.toString()?.toLongOrNull() ?: 500
        return try {
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) {
                val vm = context.getSystemService(Context.VIBRATOR_MANAGER_SERVICE) as VibratorManager
                vm.defaultVibrator.vibrate(VibrationEffect.createOneShot(duration, VibrationEffect.DEFAULT_AMPLITUDE))
            } else {
                @Suppress("DEPRECATION")
                val v = context.getSystemService(Context.VIBRATOR_SERVICE) as Vibrator
                v.vibrate(VibrationEffect.createOneShot(duration, VibrationEffect.DEFAULT_AMPLITUDE))
            }
            "Vibrated ${duration}ms" to 0
        } catch (e: Exception) {
            "Vibrate failed: ${e.message}" to 1
        }
    }

    // ---- Volume ----

    private fun execSetVolume(task: JSONObject): Pair<String, Int> {
        val level = task.opt("level")?.toString()?.toIntOrNull() ?: -1
        val stream = when (task.optString("stream", "music")) {
            "ring" -> AudioManager.STREAM_RING
            "alarm" -> AudioManager.STREAM_ALARM
            "notification" -> AudioManager.STREAM_NOTIFICATION
            else -> AudioManager.STREAM_MUSIC
        }
        val am = context.getSystemService(Context.AUDIO_SERVICE) as AudioManager
        val maxVol = am.getStreamMaxVolume(stream)

        if (level < 0 || level > maxVol) return "level must be 0-$maxVol" to 1
        am.setStreamVolume(stream, level, 0)
        return "Volume set to $level/$maxVol" to 0
    }

    // ---- Open App ----

    private fun execOpenApp(task: JSONObject): Pair<String, Int> {
        val pkg = task.optString("package", "")
        if (pkg.isEmpty()) return "Missing 'package'" to 1
        return try {
            val intent = context.packageManager.getLaunchIntentForPackage(pkg)
                ?: return "App not found: $pkg" to 1
            intent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
            context.startActivity(intent)
            "Opened $pkg" to 0
        } catch (e: Exception) {
            "Open app failed: ${e.message}" to 1
        }
    }

    // ---- Helpers ----

    private fun shellQuote(s: String): String = "'${s.replace("'", "'\\''")}'"

    private fun globToRegex(glob: String): Regex {
        val sb = StringBuilder("^")
        for (c in glob) {
            when (c) {
                '*' -> sb.append(".*")
                '?' -> sb.append(".")
                '.' -> sb.append("\\.")
                else -> sb.append(c)
            }
        }
        sb.append("$")
        return Regex(sb.toString())
    }

    // ---- Nearby Transfer ----

    private val nearbyTransfer by lazy {
        NearbyTransferManager(context).apply {
            // Agent-initiated transfers skip the user confirmation popup
            autoAcceptTransfers = true
        }
    }

    private fun execNearbyScan(task: JSONObject): Pair<String, Int> {
        val timeout = task.optLong("timeout", 10) * 1000
        return try {
            nearbyTransfer.setDeviceName(config?.terminalName ?: "octo-phone")
            nearbyTransfer.toolScan(timeout) to 0
        } catch (e: Exception) {
            "Error: ${e.message}" to 1
        }
    }

    private fun execNearbySend(task: JSONObject): Pair<String, Int> {
        val path = task.optString("path", "")
        if (path.isEmpty()) return "Missing 'path'" to 1
        val resolved = resolvePath(path)
        checkPath(resolved)?.let { return it }
        return try {
            nearbyTransfer.setDeviceName(config?.terminalName ?: "octo-phone")
            nearbyTransfer.toolSend(resolved) to 0
        } catch (e: Exception) {
            "Error: ${e.message}" to 1
        }
    }

    private fun execNearbyReceive(task: JSONObject): Pair<String, Int> {
        val address = task.optString("sender_address", "")
        if (address.isEmpty()) return "Missing 'sender_address'" to 1
        val saveDir = task.optString("save_dir", "/sdcard/Download")
        return try {
            nearbyTransfer.setDeviceName(config?.terminalName ?: "octo-phone")
            nearbyTransfer.toolReceive(address, saveDir) to 0
        } catch (e: Exception) {
            "Error: ${e.message}" to 1
        }
    }
}
