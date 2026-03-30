package com.openocto.app

import android.app.AlertDialog
import android.content.Intent
import android.os.Bundle

import android.view.HapticFeedbackConstants
import android.view.View
import android.view.inputmethod.EditorInfo
import android.widget.*
import androidx.activity.result.contract.ActivityResultContracts
import androidx.appcompat.app.AppCompatActivity
import androidx.core.view.ViewCompat
import androidx.core.view.WindowInsetsCompat
import androidx.recyclerview.widget.RecyclerView
import java.util.Locale

/**
 * On-device AI Agent — Cline-style chat with built-in octo tools.
 * Phone calls LLM API directly and executes octo commands via RelayClient.
 *
 * Display is driven by DisplayMode interface (currently Option A: ChatDisplayMode).
 * Extension slot: swap displayMode to TerminalDisplayMode or SplitDisplayMode in the future.
 */
class AgentActivity : AppCompatActivity() {

    private lateinit var config: OctoConfig
    private var relay: RelayClient? = null
    private var engine: AgentEngine? = null

    private lateinit var displayMode: DisplayMode
    private lateinit var recyclerChat: RecyclerView
    private lateinit var etInput: EditText
    private lateinit var tvStatus: TextView

    private var isBusy = false
    private var currentStreamId: String? = null
    private var lastSavedHash: Int = 0
    private var currentSessionFile: java.io.File? = null
    private var isRecording = false
    private var mediaRecorder: android.media.MediaRecorder? = null
    private var recordFile: java.io.File? = null

    private val permissionLauncher = registerForActivityResult(
        ActivityResultContracts.RequestMultiplePermissions()
    ) { _ -> }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_agent)

        config = OctoConfig(this)

        recyclerChat = findViewById(R.id.recyclerChat)
        etInput = findViewById(R.id.etInput)
        tvStatus = findViewById(R.id.tvStatus)

        // --- DisplayMode: Option A (Cline-style chat + tool cards) ---
        displayMode = ChatDisplayMode(recyclerChat)

        // Toolbar
        findViewById<com.google.android.material.appbar.MaterialToolbar>(R.id.toolbar).apply {
            setNavigationOnClickListener { finish() }
            inflateMenu(R.menu.agent_menu)
            setOnMenuItemClickListener { item ->
                when (item.itemId) {
                    R.id.menu_settings -> { showApiSettings(); true }
                    R.id.menu_clear -> { clearChat(); true }
                    R.id.menu_history -> { showSessionHistory(); true }
                    R.id.menu_memories -> { showMemories(); true }
                    R.id.menu_agent_config -> { showAgentConfigEditor(); true }
                    else -> false
                }
            }
        }

        // Send
        findViewById<com.google.android.material.button.MaterialButton>(R.id.btnSend)
            .setOnClickListener { v ->
                val text = etInput.text.toString().trim()
                if (text.isNotEmpty() && !isBusy) {
                    v.performHapticFeedback(HapticFeedbackConstants.CONFIRM)
                    sendMessage(text)
                }
            }

        // Voice
        findViewById<com.google.android.material.button.MaterialButton>(R.id.btnVoice)
            .setOnClickListener { startVoiceInput() }

        // Enter = send
        etInput.setOnEditorActionListener { _, actionId, _ ->
            if (actionId == EditorInfo.IME_ACTION_SEND) {
                val text = etInput.text.toString().trim()
                if (text.isNotEmpty() && !isBusy) sendMessage(text)
                true
            } else false
        }

        // Handle system insets: top (dynamic island) + bottom (keyboard / nav bar)
        val rootView = findViewById<View>(android.R.id.content)?.let {
            (it as? android.view.ViewGroup)?.getChildAt(0)
        }
        if (rootView != null) {
            ViewCompat.setOnApplyWindowInsetsListener(rootView) { view, insets ->
                val systemBars = insets.getInsets(WindowInsetsCompat.Type.systemBars())
                val ime = insets.getInsets(WindowInsetsCompat.Type.ime())
                val topPad = systemBars.top
                val bottomPad = maxOf(ime.bottom, systemBars.bottom)
                view.setPadding(systemBars.left, topPad, systemBars.right, bottomPad)
                if (ime.bottom > 0) {
                    displayMode.scrollToBottom()
                }
                WindowInsetsCompat.CONSUMED
            }
        }

        // Init relay + engine
        initEngine()

        // Quick command shortcuts
        setupShortcuts()

        // Welcome message
        displayMode.addMessage(ChatItem.Message(
            role = ChatItem.Role.ASSISTANT,
            text = if (config.isAiConfigured)
                "Hi! I'm your on-device agent with built-in octo tools. Ask me to run commands, read files, or manage your remote terminals."
            else
                "Welcome! Configure your API key first via the settings menu (top-right)."
        ))
    }

    override fun onPause() {
        super.onPause()
        saveSession()
    }

    private fun initEngine() {
        if (config.isConfigured) {
            relay = RelayClient(
                config.redisUrl, config.redisToken, config.workspace,
                proxyUrl = config.proxyUrl
            )
        }
        if (config.isAiConfigured && relay != null) {
            val localHandler = TaskHandler(this, relay!!, config.terminalName, config)
            engine = AgentEngine(config, relay!!, localHandler)

            // Wire confirmation callback
            (displayMode as? ChatDisplayMode)?.adapter?.onConfirmResponse = { confirmId, approved ->
                engine?.onConfirmResponse(confirmId, approved)
            }
        }
    }

    // ---- Chat ----

    private fun sendMessage(text: String) {
        etInput.setText("")
        isBusy = true

        displayMode.addMessage(ChatItem.Message(role = ChatItem.Role.USER, text = text))

        if (engine == null) {
            if (!config.isAiConfigured) {
                displayMode.addMessage(ChatItem.Message(
                    role = ChatItem.Role.ASSISTANT,
                    text = "API not configured. Tap the settings icon to set your API provider and key."
                ))
            } else if (relay == null) {
                displayMode.addMessage(ChatItem.Message(
                    role = ChatItem.Role.ASSISTANT,
                    text = "Relay not configured. Go back and scan QR code first."
                ))
            }
            isBusy = false
            return
        }

        setStatus("Thinking...")

        Thread {
            engine!!.chat(text) { event ->
                runOnUiThread {
                    when (event) {
                        is AgentEngine.Event.AssistantText -> {
                            displayMode.addMessage(ChatItem.Message(
                                role = ChatItem.Role.ASSISTANT, text = event.text
                            ))
                            setStatus(null)
                        }
                        is AgentEngine.Event.TextChunk -> {
                            if (currentStreamId != event.messageId) {
                                currentStreamId = event.messageId
                                displayMode.addStreamingMessage(ChatItem.StreamingMessage(event.messageId))
                            }
                            displayMode.appendStreamChunk(event.messageId, event.chunk)
                            setStatus(null)
                        }
                        is AgentEngine.Event.TextDone -> {
                            displayMode.completeStream(event.messageId)
                            currentStreamId = null
                        }
                        is AgentEngine.Event.ToolStart -> {
                            displayMode.addToolCall(ChatItem.ToolCall(
                                toolUseId = event.toolUseId,
                                toolName = event.toolName,
                                target = event.target,
                                args = event.args,
                                status = ChatItem.ToolStatus.RUNNING
                            ))
                            setStatus("Running ${event.toolName}...")
                        }
                        is AgentEngine.Event.ToolResult -> {
                            val status = if (event.success) ChatItem.ToolStatus.DONE
                                         else ChatItem.ToolStatus.FAILED
                            displayMode.updateToolCall(event.toolUseId, status,
                                event.result, event.durationMs)
                            setStatus(null)
                        }
                        is AgentEngine.Event.ConfirmRequired -> {
                            displayMode.addConfirmation(ChatItem.Confirmation(
                                confirmId = event.confirmId,
                                toolName = event.toolName,
                                description = event.description,
                                args = event.args
                            ))
                            setStatus("Waiting for confirmation...")
                        }
                        is AgentEngine.Event.RoundStart -> {
                            displayMode.addRoundSummary(ChatItem.RoundSummary(
                                roundId = event.roundId
                            ))
                        }
                        is AgentEngine.Event.RoundEnd -> {
                            displayMode.updateRoundSummary(
                                event.roundId, event.toolCount, event.totalMs, true
                            )
                        }
                        is AgentEngine.Event.Error -> {
                            displayMode.addMessage(ChatItem.Message(
                                role = ChatItem.Role.ASSISTANT,
                                text = "Error: ${event.message}"
                            ))
                            setStatus(null)
                            isBusy = false
                        }
                        is AgentEngine.Event.Done -> {
                            setStatus(null)
                            isBusy = false
                            saveSession()
                        }
                    }
                }
            }
        }.start()
    }

    private fun setStatus(text: String?) {
        tvStatus.text = text ?: ""
        tvStatus.visibility = if (text != null) View.VISIBLE else View.GONE
    }

    private fun clearChat() {
        saveSession()
        currentSessionFile = null  // next session gets a new file
        lastSavedHash = 0
        displayMode.clear()
        engine?.clearHistory()
        displayMode.addMessage(ChatItem.Message(
            role = ChatItem.Role.ASSISTANT,
            text = "Chat cleared. How can I help?"
        ))
    }

    // ---- Quick Shortcuts ----

    private fun setupShortcuts() {
        val container = findViewById<android.widget.LinearLayout>(R.id.layoutShortcuts)
        container.removeAllViews()

        // Default shortcuts + load from octo-agent.md
        val shortcuts = mutableListOf(
            "\uD83D\uDCCA Status" to "Show the status of all devices",
            "\uD83D\uDCF1 Device Info" to "Show this phone's device info",
            "\u23F0 Alarm" to "Set an alarm",
            "\uD83D\uDC64 Contacts" to "Search my contacts"
        )

        // Load custom shortcuts from octo-agent.md
        val agentMd = java.io.File("/sdcard/.octo/octo-agent.md")
        if (agentMd.exists()) {
            try {
                var inShortcuts = false
                for (line in agentMd.readLines()) {
                    if (line.trim().lowercase().startsWith("## shortcut")) {
                        inShortcuts = true; continue
                    }
                    if (line.trim().startsWith("##")) inShortcuts = false
                    if (inShortcuts && line.trim().startsWith("- ")) {
                        val parts = line.trim().removePrefix("- ").split(":", limit = 2)
                        if (parts.size == 2) {
                            shortcuts.add(parts[0].trim() to parts[1].trim())
                        }
                    }
                }
            } catch (_: Exception) {}
        }

        for ((label, prompt) in shortcuts) {
            val chip = com.google.android.material.chip.Chip(this).apply {
                text = label
                textSize = 12f
                isClickable = true
                setOnClickListener {
                    if (!isBusy) {
                        etInput.setText(prompt)
                        sendMessage(prompt)
                    }
                }
            }
            container.addView(chip)
        }
    }

    // ---- Session History ----

    private val sessionDir by lazy {
        java.io.File(filesDir, "sessions").apply { mkdirs() }
    }

    private fun saveSession() {
        val adapter = (displayMode as? ChatDisplayMode)?.adapter ?: return
        val items = adapter.getItems()
        if (items.size <= 1) return

        // Skip if nothing changed since last save
        val hash = items.hashCode()
        if (hash == lastSavedHash) return
        lastSavedHash = hash

        try {
            val arr = org.json.JSONArray()
            for (item in items) {
                when (item) {
                    is ChatItem.Message -> arr.put(org.json.JSONObject().apply {
                        put("type", "message")
                        put("role", item.role.name)
                        put("text", item.text)
                        put("ts", item.timestamp)
                    })
                    is ChatItem.StreamingMessage -> arr.put(org.json.JSONObject().apply {
                        put("type", "message")
                        put("role", "ASSISTANT")
                        put("text", item.text.toString())
                        put("ts", item.timestamp)
                    })
                    is ChatItem.ToolCall -> arr.put(org.json.JSONObject().apply {
                        put("type", "tool")
                        put("name", item.toolName)
                        put("target", item.target ?: "")
                        put("status", item.status.name)
                        put("duration", item.durationMs)
                        put("result", (item.result ?: "").take(500))
                        put("args", org.json.JSONObject(item.args as Map<*, *>))
                        put("ts", item.timestamp)
                    })
                    else -> {} // skip RoundSummary, Confirmation
                }
            }
            // Reuse current session file or create new one
            if (currentSessionFile == null) {
                currentSessionFile = java.io.File(sessionDir, "session_${System.currentTimeMillis()}.json")
            }
            currentSessionFile!!.writeText(arr.toString(2))
        } catch (e: Exception) {
            android.util.Log.e("AgentActivity", "Save session failed: ${e.message}")
        }
    }

    private data class SessionInfo(
        val file: java.io.File,
        val title: String,
        val preview: String,
        val time: Long,
        val msgCount: Int,
        val toolCount: Int
    )

    private fun parseSessionFile(file: java.io.File): SessionInfo? {
        return try {
            val arr = org.json.JSONArray(file.readText())
            if (arr.length() == 0) return null
            var title = ""
            var preview = ""
            var msgCount = 0
            var toolCount = 0
            for (i in 0 until arr.length()) {
                val obj = arr.getJSONObject(i)
                val type = obj.optString("type")
                if (type == "message") {
                    msgCount++
                    val text = obj.optString("text", "")
                    if (obj.optString("role") == "USER" && title.isEmpty()) {
                        title = text.take(50)
                    }
                    if (obj.optString("role") == "ASSISTANT" && preview.isEmpty() && text.length > 10) {
                        preview = text.take(80)
                    }
                } else if (type == "tool") {
                    toolCount++
                }
            }
            if (title.isEmpty()) title = "Untitled"
            SessionInfo(file, title, preview, file.lastModified(), msgCount, toolCount)
        } catch (_: Exception) { null }
    }

    private fun showSessionHistory() {
        val sessions = (sessionDir.listFiles() ?: emptyArray())
            .filter { it.extension == "json" }
            .sortedByDescending { it.lastModified() }
            .take(50)
            .mapNotNull { parseSessionFile(it) }

        if (sessions.isEmpty()) {
            Toast.makeText(this, "No saved sessions", Toast.LENGTH_SHORT).show()
            return
        }

        // Group by date
        val now = System.currentTimeMillis()
        val todayStart = java.util.Calendar.getInstance().apply {
            set(java.util.Calendar.HOUR_OF_DAY, 0); set(java.util.Calendar.MINUTE, 0)
            set(java.util.Calendar.SECOND, 0); set(java.util.Calendar.MILLISECOND, 0)
        }.timeInMillis
        val yesterdayStart = todayStart - 86400000L
        val weekStart = todayStart - 7 * 86400000L

        val grouped = linkedMapOf<String, MutableList<SessionInfo>>()
        for (s in sessions) {
            val group = when {
                s.time >= todayStart -> "Today"
                s.time >= yesterdayStart -> "Yesterday"
                s.time >= weekStart -> "This Week"
                else -> "Earlier"
            }
            grouped.getOrPut(group) { mutableListOf() }.add(s)
        }

        // Build dialog with custom layout
        val layout = android.widget.LinearLayout(this).apply {
            orientation = android.widget.LinearLayout.VERTICAL
        }

        val scrollView = android.widget.ScrollView(this).apply {
            layoutParams = android.widget.LinearLayout.LayoutParams(
                android.widget.LinearLayout.LayoutParams.MATCH_PARENT,
                (resources.displayMetrics.heightPixels * 0.6).toInt()
            )
        }

        val container = android.widget.LinearLayout(this).apply {
            orientation = android.widget.LinearLayout.VERTICAL
        }

        val timeFmt = java.text.SimpleDateFormat("HH:mm", java.util.Locale.getDefault())
        val dateFmt = java.text.SimpleDateFormat("MM/dd HH:mm", java.util.Locale.getDefault())

        for ((group, items) in grouped) {
            // Group header
            val header = android.widget.TextView(this).apply {
                text = group
                textSize = 12f
                setTextColor(getColor(R.color.on_surface_variant))
                setPadding(20.dp, 16.dp, 20.dp, 4.dp)
                setTypeface(null, android.graphics.Typeface.BOLD)
            }
            container.addView(header)

            for (session in items) {
                val view = layoutInflater.inflate(R.layout.item_session, container, false)
                view.findViewById<android.widget.TextView>(R.id.tvTitle).text = session.title
                view.findViewById<android.widget.TextView>(R.id.tvTime).text =
                    if (session.time >= todayStart) timeFmt.format(java.util.Date(session.time))
                    else dateFmt.format(java.util.Date(session.time))
                view.findViewById<android.widget.TextView>(R.id.tvPreview).text = session.preview
                view.findViewById<android.widget.TextView>(R.id.tvStats).text =
                    "${session.msgCount} messages" +
                    if (session.toolCount > 0) " · ${session.toolCount} tools" else ""

                view.setOnClickListener {
                    loadSession(session.file)
                    (view.parent?.parent?.parent as? android.app.Dialog)?.dismiss()
                }
                view.findViewById<android.widget.ImageView>(R.id.btnDelete).setOnClickListener {
                    session.file.delete()
                    container.removeView(view)
                    Toast.makeText(this, "Deleted", Toast.LENGTH_SHORT).show()
                }
                container.addView(view)

                // Divider
                val divider = android.view.View(this).apply {
                    layoutParams = android.widget.LinearLayout.LayoutParams(
                        android.widget.LinearLayout.LayoutParams.MATCH_PARENT, 1
                    ).apply { marginStart = 20.dp; marginEnd = 20.dp }
                    setBackgroundColor(getColor(R.color.outline))
                }
                container.addView(divider)
            }
        }

        scrollView.addView(container)
        layout.addView(scrollView)

        android.app.AlertDialog.Builder(this)
            .setTitle("History")
            .setView(layout)
            .setNegativeButton("Close", null)
            .show()
    }

    private val Int.dp: Int get() = (this * resources.displayMetrics.density).toInt()

    // ---- Memory Management ----

    private fun showMemories() {
        val memDir = java.io.File("/sdcard/.octo/memories")
        val files = (memDir.listFiles() ?: emptyArray())
            .filter { it.extension == "md" }
            .sortedByDescending { it.lastModified() }

        val layout = android.widget.LinearLayout(this).apply {
            orientation = android.widget.LinearLayout.VERTICAL
        }

        val scroll = android.widget.ScrollView(this).apply {
            layoutParams = android.widget.LinearLayout.LayoutParams(
                android.widget.LinearLayout.LayoutParams.MATCH_PARENT,
                (resources.displayMetrics.heightPixels * 0.5).toInt()
            )
        }

        val container = android.widget.LinearLayout(this).apply {
            orientation = android.widget.LinearLayout.VERTICAL
            setPadding(20.dp, 12.dp, 20.dp, 12.dp)
        }

        if (files.isEmpty()) {
            container.addView(android.widget.TextView(this).apply {
                text = "No memories saved yet.\nThe agent will save important info automatically during conversations."
                textSize = 14f
                setTextColor(getColor(R.color.on_surface_variant))
                setPadding(0, 16.dp, 0, 16.dp)
            })
        }

        for (file in files) {
            val content = try { file.readText().take(200) } catch (_: Exception) { "" }
            val row = android.widget.LinearLayout(this).apply {
                orientation = android.widget.LinearLayout.HORIZONTAL
                gravity = android.view.Gravity.CENTER_VERTICAL
                setPadding(0, 10.dp, 0, 10.dp)
            }

            val textCol = android.widget.LinearLayout(this).apply {
                orientation = android.widget.LinearLayout.VERTICAL
                layoutParams = android.widget.LinearLayout.LayoutParams(0,
                    android.widget.LinearLayout.LayoutParams.WRAP_CONTENT, 1f)
            }
            textCol.addView(android.widget.TextView(this).apply {
                text = "\uD83E\uDDE0 ${file.nameWithoutExtension}"
                textSize = 14f
                setTextColor(getColor(R.color.on_surface))
            })
            textCol.addView(android.widget.TextView(this).apply {
                text = content.replace("\n", " ").take(100)
                textSize = 12f
                setTextColor(getColor(R.color.on_surface_variant))
                maxLines = 2
            })
            row.addView(textCol)

            val deleteBtn = android.widget.ImageView(this).apply {
                setImageResource(android.R.drawable.ic_menu_delete)
                alpha = 0.4f
                setPadding(8.dp, 8.dp, 8.dp, 8.dp)
                setOnClickListener {
                    file.delete()
                    container.removeView(row)
                    Toast.makeText(this@AgentActivity, "Deleted: ${file.nameWithoutExtension}", Toast.LENGTH_SHORT).show()
                }
            }
            row.addView(deleteBtn)
            container.addView(row)

            // Divider
            container.addView(android.view.View(this).apply {
                layoutParams = android.widget.LinearLayout.LayoutParams(
                    android.widget.LinearLayout.LayoutParams.MATCH_PARENT, 1)
                setBackgroundColor(getColor(R.color.outline))
            })
        }

        scroll.addView(container)
        layout.addView(scroll)

        android.app.AlertDialog.Builder(this)
            .setTitle("Memories (${files.size})")
            .setView(layout)
            .setNegativeButton("Close", null)
            .show()
    }

    // ---- Agent Config Editor (octo-agent.md) ----

    private fun showAgentConfigEditor() {
        val agentFile = java.io.File("/sdcard/.octo/octo-agent.md")
        val currentContent = try {
            if (agentFile.exists()) agentFile.readText() else ""
        } catch (_: Exception) { "" }

        val layout = android.widget.LinearLayout(this).apply {
            orientation = android.widget.LinearLayout.VERTICAL
            setPadding(20.dp, 12.dp, 20.dp, 0)
        }

        layout.addView(android.widget.TextView(this).apply {
            text = "Edit /sdcard/.octo/octo-agent.md\nThe agent reads this at the start of every conversation."
            textSize = 12f
            setTextColor(getColor(R.color.on_surface_variant))
            setPadding(0, 0, 0, 8.dp)
        })

        val editText = android.widget.EditText(this).apply {
            setText(currentContent.ifEmpty {
                "# octo-agent\nname: MyPhone\nrole: Personal assistant\n\n## Rules\n- Respond in Chinese\n\n## Shortcuts\n- GPU Status: Check nvidia-smi on gpu-server\n"
            })
            textSize = 13f
            setTypeface(android.graphics.Typeface.MONOSPACE)
            gravity = android.view.Gravity.TOP
            minLines = 12
            maxLines = 20
            inputType = android.text.InputType.TYPE_CLASS_TEXT or
                    android.text.InputType.TYPE_TEXT_FLAG_MULTI_LINE or
                    android.text.InputType.TYPE_TEXT_FLAG_NO_SUGGESTIONS
            setBackgroundResource(R.drawable.bg_terminal_output)
            setTextColor(0xFFC9D1D9.toInt())
            setPadding(12.dp, 12.dp, 12.dp, 12.dp)
        }
        layout.addView(editText)

        android.app.AlertDialog.Builder(this)
            .setTitle("Agent Config")
            .setView(layout)
            .setPositiveButton("Save") { _, _ ->
                try {
                    agentFile.parentFile?.mkdirs()
                    agentFile.writeText(editText.text.toString())
                    Toast.makeText(this, "Saved. Takes effect on next conversation.", Toast.LENGTH_SHORT).show()
                    // Reload shortcuts
                    setupShortcuts()
                } catch (e: Exception) {
                    Toast.makeText(this, "Save failed: ${e.message}", Toast.LENGTH_SHORT).show()
                }
            }
            .setNegativeButton("Cancel", null)
            .show()
    }

    private fun loadSession(file: java.io.File) {
        try {
            val arr = org.json.JSONArray(file.readText())
            displayMode.clear()
            engine?.clearHistory()
            for (i in 0 until arr.length()) {
                val obj = arr.getJSONObject(i)
                when (obj.optString("type")) {
                    "message" -> {
                        val role = if (obj.optString("role") == "USER") ChatItem.Role.USER
                                   else ChatItem.Role.ASSISTANT
                        displayMode.addMessage(ChatItem.Message(role = role, text = obj.optString("text", "")))
                    }
                    "tool" -> {
                        val status = try { ChatItem.ToolStatus.valueOf(obj.optString("status", "DONE")) }
                                     catch (_: Exception) { ChatItem.ToolStatus.DONE }
                        val argsObj = obj.optJSONObject("args")
                        val argsMap = mutableMapOf<String, String>()
                        if (argsObj != null) {
                            for (k in argsObj.keys()) argsMap[k] = argsObj.optString(k, "")
                        }
                        val tc = ChatItem.ToolCall(
                            toolUseId = "hist-$i", toolName = obj.optString("name"),
                            target = obj.optString("target", "").ifEmpty { null },
                            args = argsMap, status = status,
                            durationMs = obj.optLong("duration", 0),
                            result = obj.optString("result", "").ifEmpty { null }
                        )
                        displayMode.addToolCall(tc)
                    }
                }
            }
            Toast.makeText(this, "Session loaded", Toast.LENGTH_SHORT).show()
        } catch (e: Exception) {
            Toast.makeText(this, "Load failed: ${e.message}", Toast.LENGTH_SHORT).show()
        }
    }

    // ---- Voice ----

    private fun startVoiceInput() {
        if (isRecording) {
            stopRecordingAndTranscribe()
            return
        }

        // Check microphone permission
        if (androidx.core.content.ContextCompat.checkSelfPermission(this,
                android.Manifest.permission.RECORD_AUDIO) != android.content.pm.PackageManager.PERMISSION_GRANTED) {
            permissionLauncher.launch(arrayOf(android.Manifest.permission.RECORD_AUDIO))
            return
        }

        // Start recording
        try {
            recordFile = java.io.File(cacheDir, "voice_${System.currentTimeMillis()}.m4a")
            mediaRecorder = (if (android.os.Build.VERSION.SDK_INT >= android.os.Build.VERSION_CODES.S)
                android.media.MediaRecorder(this) else @Suppress("DEPRECATION") android.media.MediaRecorder()
            ).apply {
                setAudioSource(android.media.MediaRecorder.AudioSource.MIC)
                setOutputFormat(android.media.MediaRecorder.OutputFormat.MPEG_4)
                setAudioEncoder(android.media.MediaRecorder.AudioEncoder.AAC)
                setAudioSamplingRate(16000)
                setAudioChannels(1)
                setOutputFile(recordFile!!.absolutePath)
                prepare()
                start()
            }
            isRecording = true

            // Update mic button appearance
            findViewById<com.google.android.material.button.MaterialButton>(R.id.btnVoice).apply {
                setIconResource(R.drawable.ic_send)
                setBackgroundColor(getColor(R.color.status_error))
            }
            tvStatus.text = "Recording... tap mic to stop"
            tvStatus.visibility = android.view.View.VISIBLE

            // Auto-stop after 30 seconds
            android.os.Handler(android.os.Looper.getMainLooper()).postDelayed({
                if (isRecording) stopRecordingAndTranscribe()
            }, 30_000)

        } catch (e: Exception) {
            Toast.makeText(this, "Recording failed: ${e.message}", Toast.LENGTH_SHORT).show()
        }
    }

    private fun stopRecordingAndTranscribe() {
        if (!isRecording) return
        isRecording = false

        // Reset button
        findViewById<com.google.android.material.button.MaterialButton>(R.id.btnVoice).apply {
            setIconResource(R.drawable.ic_mic)
            setBackgroundColor(0) // reset to default
        }

        // Stop recording
        try {
            mediaRecorder?.stop()
            mediaRecorder?.release()
        } catch (_: Exception) {}
        mediaRecorder = null

        val audioFile = recordFile ?: return
        if (!audioFile.exists() || audioFile.length() < 1000) {
            Toast.makeText(this, "Recording too short", Toast.LENGTH_SHORT).show()
            audioFile.delete()
            return
        }

        tvStatus.text = "Transcribing..."
        tvStatus.visibility = android.view.View.VISIBLE

        // Send to ASR API in background
        Thread {
            try {
                val text = transcribeAudio(audioFile)
                audioFile.delete()
                runOnUiThread {
                    tvStatus.visibility = android.view.View.GONE
                    if (text.isNotBlank()) {
                        etInput.setText(text)
                        sendMessage(text)
                    } else {
                        Toast.makeText(this, "No speech detected", Toast.LENGTH_SHORT).show()
                    }
                }
            } catch (e: Exception) {
                audioFile.delete()
                runOnUiThread {
                    tvStatus.visibility = android.view.View.GONE
                    Toast.makeText(this, "Transcription failed: ${e.message}", Toast.LENGTH_SHORT).show()
                }
            }
        }.start()
    }

    private fun transcribeAudio(file: java.io.File): String {
        // Priority: proxy URL (free, no key needed) > provider API (needs key)
        val useProxy = config.proxyUrl.isNotEmpty()
        val baseUrl: String
        val apiKey: String
        val model: String

        if (useProxy) {
            // Use CF Worker proxy — token is server-side, free for users
            baseUrl = config.proxyUrl.trimEnd('/')
            apiKey = "" // no key needed, worker handles auth
            model = "FunAudioLLM/SenseVoiceSmall"
        } else {
            apiKey = config.aiApiKey
            baseUrl = when (config.aiProvider) {
                "siliconflow" -> "https://api.siliconflow.cn/v1"
                "openai" -> config.aiBaseUrl.ifEmpty { "https://api.openai.com/v1" }
                else -> config.aiBaseUrl.ifEmpty { "https://api.siliconflow.cn/v1" }
            }.trimEnd('/')
            model = when (config.aiProvider) {
                "siliconflow" -> "FunAudioLLM/SenseVoiceSmall"
                "openai" -> "whisper-1"
                else -> "FunAudioLLM/SenseVoiceSmall"
            }
        }

        val boundary = "----OctoVoice${System.currentTimeMillis()}"
        val crlf = "\r\n"

        val bodyStream = java.io.ByteArrayOutputStream()
        val writer = java.io.OutputStreamWriter(bodyStream)

        // model field
        writer.append("--$boundary$crlf")
        writer.append("Content-Disposition: form-data; name=\"model\"$crlf$crlf")
        writer.append("$model$crlf")

        // file field
        writer.append("--$boundary$crlf")
        writer.append("Content-Disposition: form-data; name=\"file\"; filename=\"${file.name}\"$crlf")
        writer.append("Content-Type: audio/mp4$crlf$crlf")
        writer.flush()
        bodyStream.write(file.readBytes())
        writer.append("$crlf--$boundary--$crlf")
        writer.flush()

        val body = bodyStream.toByteArray()

        val url = java.net.URL("$baseUrl/audio/transcriptions")
        val conn = url.openConnection() as java.net.HttpURLConnection
        conn.requestMethod = "POST"
        conn.doOutput = true
        conn.connectTimeout = 15000
        conn.readTimeout = 30000
        if (apiKey.isNotEmpty()) {
            conn.setRequestProperty("Authorization", "Bearer $apiKey")
        }
        conn.setRequestProperty("Content-Type", "multipart/form-data; boundary=$boundary")
        conn.outputStream.use { it.write(body) }

        val respCode = conn.responseCode
        val respBody = (if (respCode in 200..299) conn.inputStream else conn.errorStream)
            .bufferedReader().readText()

        if (respCode !in 200..299) {
            throw Exception("ASR API error $respCode: $respBody")
        }

        return org.json.JSONObject(respBody).optString("text", "").trim()
    }

    // ---- API Settings ----

    private fun showApiSettings() {
        val layout = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(48, 32, 48, 16)
        }

        // Provider
        val providerLabel = TextView(this).apply { text = "Provider"; textSize = 14f }
        layout.addView(providerLabel)
        val providers = arrayOf("claude", "openai", "gemini", "ollama", "openrouter", "siliconflow", "qwen", "kimi", "minimax")
        val providerSpinner = Spinner(this)
        providerSpinner.adapter = ArrayAdapter(this, android.R.layout.simple_spinner_dropdown_item, providers)
        val currentIdx = providers.indexOf(config.aiProvider).coerceAtLeast(0)
        providerSpinner.setSelection(currentIdx)
        layout.addView(providerSpinner)

        // API Key
        val keyLabel = TextView(this).apply { text = "API Key"; textSize = 14f; setPadding(0, 24, 0, 4) }
        layout.addView(keyLabel)
        val keyInput = EditText(this).apply {
            setText(config.aiApiKey)
            hint = "sk-..."
            inputType = android.text.InputType.TYPE_CLASS_TEXT or android.text.InputType.TYPE_TEXT_VARIATION_PASSWORD
            isSingleLine = true
        }
        layout.addView(keyInput)

        // Model
        val modelLabel = TextView(this).apply { text = "Model (optional)"; textSize = 14f; setPadding(0, 24, 0, 4) }
        layout.addView(modelLabel)
        val modelInput = EditText(this).apply {
            setText(config.aiModel)
            hint = "Leave empty for default"
            isSingleLine = true
        }
        layout.addView(modelInput)

        // Base URL
        val urlLabel = TextView(this).apply { text = "Base URL (optional)"; textSize = 14f; setPadding(0, 24, 0, 4) }
        layout.addView(urlLabel)
        val urlInput = EditText(this).apply {
            setText(config.aiBaseUrl)
            hint = "Leave empty for default"
            isSingleLine = true
        }
        layout.addView(urlInput)

        AlertDialog.Builder(this)
            .setTitle("API Settings")
            .setView(layout)
            .setPositiveButton("Save") { _, _ ->
                config.aiProvider = providers[providerSpinner.selectedItemPosition]
                config.aiApiKey = keyInput.text.toString().trim()
                config.aiModel = modelInput.text.toString().trim()
                config.aiBaseUrl = urlInput.text.toString().trim()
                // Reinitialize engine
                initEngine()
                Toast.makeText(this, "API settings saved", Toast.LENGTH_SHORT).show()
            }
            .setNegativeButton("Cancel", null)
            .show()
    }
}
