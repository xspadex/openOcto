package com.openocto.app

import android.content.Intent
import android.os.Bundle
import android.view.HapticFeedbackConstants
import android.view.LayoutInflater
import android.view.View
import android.view.ViewGroup
import android.view.inputmethod.EditorInfo
import android.widget.EditText
import android.widget.LinearLayout
import android.widget.TextView
import android.widget.Toast
import androidx.activity.result.contract.ActivityResultContracts
import androidx.fragment.app.Fragment
import androidx.recyclerview.widget.RecyclerView
import java.util.Locale

/**
 * Octo AI chat fragment — the primary experience.
 * Works without relay (local-only mode), enhanced with relay (remote control).
 */
class OctoFragment : Fragment() {

    private lateinit var config: OctoConfig
    private lateinit var petManager: PetManager
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

    private val sessionDir by lazy {
        java.io.File(requireContext().filesDir, "sessions").apply { mkdirs() }
    }

    private val permissionLauncher = registerForActivityResult(
        ActivityResultContracts.RequestMultiplePermissions()
    ) { _ -> }

    override fun onCreateView(inflater: LayoutInflater, container: ViewGroup?, savedInstanceState: Bundle?): View {
        return inflater.inflate(R.layout.fragment_octo, container, false)
    }

    override fun onViewCreated(view: View, savedInstanceState: Bundle?) {
        super.onViewCreated(view, savedInstanceState)
        config = OctoConfig(requireContext())
        petManager = PetManager(requireContext())

        recyclerChat = view.findViewById(R.id.recyclerChat)
        etInput = view.findViewById(R.id.etInput)
        tvStatus = view.findViewById(R.id.tvStatus)

        displayMode = ChatDisplayMode(recyclerChat)

        // Send
        view.findViewById<com.google.android.material.button.MaterialButton>(R.id.btnSend)
            .setOnClickListener { v ->
                val text = etInput.text.toString().trim()
                if (text.isNotEmpty() && !isBusy) {
                    v.performHapticFeedback(HapticFeedbackConstants.CONFIRM)
                    sendMessage(text)
                }
            }

        // Voice
        view.findViewById<com.google.android.material.button.MaterialButton>(R.id.btnVoice)
            .setOnClickListener { startVoiceInput() }

        // Enter = send
        etInput.setOnEditorActionListener { _, actionId, _ ->
            if (actionId == EditorInfo.IME_ACTION_SEND) {
                val text = etInput.text.toString().trim()
                if (text.isNotEmpty() && !isBusy) sendMessage(text)
                true
            } else false
        }

        initEngine()
        setupShortcuts()

        // Welcome with current pet's pixel art
        if ((displayMode as? ChatDisplayMode)?.adapter?.itemCount == 0) {
            showPetWelcome()
        }

        // Update toolbar with current pet info
        updateToolbarPet()
    }

    override fun onPause() {
        super.onPause()
        saveSession()
    }

    fun initEngine() {
        if (!isAdded) return
        config = OctoConfig(requireContext())

        relay = if (config.isConfigured) {
            RelayClient(config.redisUrl, config.redisToken, config.workspace,
                proxyUrl = config.proxyUrl)
        } else null

        if (config.isAiConfigured) {
            val localHandler = if (relay != null)
                TaskHandler(requireContext(), relay!!, config.terminalName, config)
            else null
            engine = if (relay != null)
                AgentEngine(config, relay!!, localHandler, requireContext().applicationContext,
                    currentPet = petManager.getCurrentPet())
            else {
                null
            }

            // Wire confirmation callback
            (displayMode as? ChatDisplayMode)?.adapter?.onConfirmResponse = { confirmId, approved ->
                engine?.onConfirmResponse(confirmId, approved)
            }
        }
    }

    private fun sendMessage(text: String) {
        etInput.setText("")
        isBusy = true
        petManager.incrementChats()

        // Check for new unlocks
        val newPets = petManager.checkUnlocks()
        for (np in newPets) {
            displayMode.addMessage(ChatItem.Message(
                role = ChatItem.Role.ASSISTANT,
                text = "\n\n\u2728 New pet unlocked! \u2728\n${np.name}: \"${np.greetings.first()}\"",
                spannablePrefix = PetPixelArt.buildSmall(np.species, np.color)
            ))
        }

        displayMode.addMessage(ChatItem.Message(role = ChatItem.Role.USER, text = text))

        if (engine == null) {
            displayMode.addMessage(ChatItem.Message(
                role = ChatItem.Role.ASSISTANT,
                text = if (!config.isAiConfigured)
                    "AI not configured yet. Tap \u2699\uFE0F in the top right to set up a provider."
                else
                    "Relay not connected. Go to Devices tab and scan QR to connect."
            ))
            isBusy = false
            return
        }

        setStatus("Thinking...")

        Thread {
            engine!!.chat(text) { event ->
                if (!isAdded) return@chat
                requireActivity().runOnUiThread {
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
                            displayMode.addRoundSummary(ChatItem.RoundSummary(roundId = event.roundId))
                        }
                        is AgentEngine.Event.RoundEnd -> {
                            displayMode.updateRoundSummary(
                                event.roundId, event.toolCount, event.totalMs, true)
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

    fun clearChat() {
        saveSession()
        currentSessionFile = null
        lastSavedHash = 0
        displayMode.clear()
        engine?.clearHistory()
        displayMode.addMessage(ChatItem.Message(
            role = ChatItem.Role.ASSISTANT, text = getString(R.string.chat_cleared)
        ))
    }

    // ---- Pet System ----

    private fun showPetWelcome() {
        val pet = petManager.getCurrentPet()
        val art = PetPixelArt.build(pet.species, pet.color)
        val level = petManager.getLevel(pet.id)
        val levelEmoji = petManager.getLevelLabel(level)
        val greeting = pet.greetings.random()
        val welcome = if (!config.isAiConfigured)
            "\n$levelEmoji ${pet.name}\n$greeting\n\n${getString(R.string.setup_ai_hint)}"
        else
            "\n$levelEmoji ${pet.name}\n$greeting"
        displayMode.addMessage(ChatItem.Message(
            role = ChatItem.Role.ASSISTANT,
            text = welcome,
            spannablePrefix = art
        ))
    }

    private fun updateToolbarPet() {
        val pet = petManager.getCurrentPet()
        val level = petManager.getLevel(pet.id)
        val emoji = petManager.getLevelLabel(level)
        (activity as? MainActivity)?.updateToolbarTitle("$emoji ${pet.name}")
    }

    fun showPetPicker() {
        val ctx = context ?: return
        val scrollView = android.widget.ScrollView(ctx)
        val layout = android.widget.LinearLayout(ctx).apply {
            orientation = android.widget.LinearLayout.VERTICAL
            setPadding(dp(16), dp(12), dp(16), dp(8))
        }
        scrollView.addView(layout)

        // Create dialog first so we can reference it in click listeners
        val dialog = android.app.AlertDialog.Builder(ctx)
            .setTitle(getString(R.string.my_pets))
            .setView(scrollView)
            .setNegativeButton(getString(R.string.close), null)
            .create()

        // Only show Octo for now (multi-pet system planned for future)
        val visiblePets = Pet.PRESETS.filter { it.id == "octo" }
        for (pet in visiblePets) {
            val unlocked = petManager.isUnlocked(pet.id)
            val isCurrent = pet.id == petManager.getCurrentPetId()
            val level = petManager.getLevel(pet.id)

            val row = android.widget.LinearLayout(ctx).apply {
                orientation = android.widget.LinearLayout.HORIZONTAL
                gravity = android.view.Gravity.CENTER_VERTICAL
                setPadding(dp(8), dp(12), dp(8), dp(12))
                if (isCurrent) {
                    setBackgroundColor(0x2000E5FF)
                }
            }

            // Small pixel art or lock
            val artView = android.widget.TextView(ctx).apply {
                typeface = android.graphics.Typeface.MONOSPACE
                textSize = 6f
                text = if (unlocked) {
                    PetPixelArt.buildSmall(pet.species, pet.color)
                } else {
                    PetPixelArt.buildSmall(pet.species, 0xFF444444.toInt())
                }
                layoutParams = android.widget.LinearLayout.LayoutParams(dp(56), dp(40))
            }
            row.addView(artView)

            // Info
            val info = android.widget.LinearLayout(ctx).apply {
                orientation = android.widget.LinearLayout.VERTICAL
                setPadding(dp(12), 0, 0, 0)
                layoutParams = android.widget.LinearLayout.LayoutParams(0,
                    android.widget.LinearLayout.LayoutParams.WRAP_CONTENT, 1f)
            }
            info.addView(android.widget.TextView(ctx).apply {
                if (unlocked) {
                    text = "${petManager.getLevelLabel(level)} ${pet.name}"
                    setTextColor(0xFFEEEEFF.toInt())
                } else {
                    text = "\uD83D\uDD12 ???"
                    setTextColor(0xFF888888.toInt())
                }
                textSize = 15f
                setTypeface(typeface, android.graphics.Typeface.BOLD)
            })
            info.addView(android.widget.TextView(ctx).apply {
                text = if (unlocked) pet.description else petManager.getUnlockHint(pet)
                textSize = 12f
                setTextColor(if (unlocked) 0xFFAAAAAA.toInt() else 0xFF666666.toInt())
            })
            row.addView(info)

            // Current pet indicator
            if (isCurrent) {
                row.addView(android.widget.TextView(ctx).apply {
                    text = "\u2714"
                    textSize = 18f
                    setTextColor(0xFF00E5FF.toInt())
                })
            }

            if (unlocked && !isCurrent) {
                row.setOnClickListener {
                    dialog.dismiss()
                    petManager.setCurrentPet(pet.id)
                    initEngine()
                    clearChat()
                    showPetWelcome()
                    setupShortcuts()
                    updateToolbarPet()
                }
            }

            layout.addView(row)
        }

        // Divider
        layout.addView(android.view.View(ctx).apply {
            layoutParams = android.widget.LinearLayout.LayoutParams(
                android.widget.LinearLayout.LayoutParams.MATCH_PARENT, 1
            ).apply { topMargin = dp(8); bottomMargin = dp(8) }
            setBackgroundColor(0xFF3A3460.toInt())
        })

        // Multi-pet features hidden for now (code preserved for future)
        // layout.addView(android.widget.TextView(ctx).apply {
        //     text = getString(R.string.create_custom_pet)
        //     ...
        // })

        // History link
        layout.addView(android.widget.TextView(ctx).apply {
            text = "\uD83D\uDCDD  ${getString(R.string.chat_history)}"
            textSize = 14f
            setTextColor(0xFFCCCCDD.toInt())
            setPadding(dp(8), dp(12), dp(8), dp(12))
            setOnClickListener { dialog.dismiss(); showSessionHistory() }
        })

        dialog.show()
    }

    private fun showCreatePet() {
        val ctx = context ?: return
        val layout = android.widget.LinearLayout(ctx).apply {
            orientation = android.widget.LinearLayout.VERTICAL
            setPadding(dp(20), dp(16), dp(20), dp(8))
        }

        val nameInput = android.widget.EditText(ctx).apply {
            hint = "Pet name"; isSingleLine = true
        }
        layout.addView(labeledView("Name", nameInput))

        val skillInput = android.widget.EditText(ctx).apply {
            hint = "e.g. Help me manage reading notes"; isSingleLine = true
        }
        layout.addView(labeledView("What is it good at?", skillInput))

        val personalityInput = android.widget.EditText(ctx).apply {
            hint = "e.g. Quiet, organized, sometimes sarcastic"; isSingleLine = true
        }
        layout.addView(labeledView("Personality (optional)", personalityInput))

        android.app.AlertDialog.Builder(ctx)
            .setTitle("Create Pet")
            .setView(layout)
            .setPositiveButton("Create") { _, _ ->
                val name = nameInput.text.toString().trim()
                if (name.isEmpty()) return@setPositiveButton
                val skill = skillInput.text.toString().trim()
                val personality = personalityInput.text.toString().trim()

                // TODO: Store custom pets in SharedPreferences/Redis
                // For now, just switch to a custom context
                android.widget.Toast.makeText(ctx,
                    "Custom pets coming soon! Use Octo for now.",
                    android.widget.Toast.LENGTH_SHORT).show()
            }
            .setNegativeButton("Cancel", null)
            .show()
    }

    private fun labeledView(label: String, view: android.view.View): android.widget.LinearLayout {
        return android.widget.LinearLayout(requireContext()).apply {
            orientation = android.widget.LinearLayout.VERTICAL
            addView(android.widget.TextView(context).apply {
                text = label; textSize = 12f; setTextColor(0xFFCCCCDD.toInt())
                setPadding(0, dp(8), 0, dp(4))
            })
            addView(view)
        }
    }

    private fun dp(value: Int): Int = (value * resources.displayMetrics.density).toInt()

    // ---- Shortcuts ----

    private fun setupShortcuts() {
        val container = view?.findViewById<LinearLayout>(R.id.layoutShortcuts) ?: return
        container.removeAllViews()

        // Use current pet's shortcuts
        val pet = petManager.getCurrentPet()
        val shortcuts = pet.shortcuts.toMutableList()

        val agentMd = java.io.File("/sdcard/.octo/octo-agent.md")
        if (agentMd.exists()) {
            try {
                var inShortcuts = false
                for (line in agentMd.readLines()) {
                    if (line.trim().lowercase().startsWith("## shortcut")) { inShortcuts = true; continue }
                    if (line.trim().startsWith("##")) inShortcuts = false
                    if (inShortcuts && line.trim().startsWith("- ")) {
                        val parts = line.trim().removePrefix("- ").split(":", limit = 2)
                        if (parts.size == 2) shortcuts.add(parts[0].trim() to parts[1].trim())
                    }
                }
            } catch (_: Exception) {}
        }

        for ((label, prompt) in shortcuts) {
            val chip = com.google.android.material.chip.Chip(requireContext()).apply {
                text = label
                textSize = 12f
                isClickable = true
                setOnClickListener {
                    if (!isBusy) { etInput.setText(prompt); sendMessage(prompt) }
                }
            }
            container.addView(chip)
        }
    }

    // ---- Session ----

    private fun saveSession() {
        val adapter = (displayMode as? ChatDisplayMode)?.adapter ?: return
        val items = adapter.getItems()
        if (items.size <= 1) return
        val hash = items.hashCode()
        if (hash == lastSavedHash) return
        lastSavedHash = hash
        try {
            val arr = org.json.JSONArray()
            for (item in items) {
                when (item) {
                    is ChatItem.Message -> arr.put(org.json.JSONObject().apply {
                        put("type", "message"); put("role", item.role.name)
                        put("text", item.text); put("ts", item.timestamp)
                    })
                    is ChatItem.StreamingMessage -> arr.put(org.json.JSONObject().apply {
                        put("type", "message"); put("role", "ASSISTANT")
                        put("text", item.text.toString()); put("ts", item.timestamp)
                    })
                    is ChatItem.ToolCall -> arr.put(org.json.JSONObject().apply {
                        put("type", "tool"); put("name", item.toolName)
                        put("target", item.target ?: ""); put("status", item.status.name)
                        put("duration", item.durationMs)
                        put("result", (item.result ?: "").take(500))
                        put("args", org.json.JSONObject(item.args as Map<*, *>))
                        put("ts", item.timestamp)
                    })
                    else -> {}
                }
            }
            if (currentSessionFile == null)
                currentSessionFile = java.io.File(sessionDir, "session_${System.currentTimeMillis()}.json")
            currentSessionFile!!.writeText(arr.toString(2))
        } catch (_: Exception) {}
    }

    fun showSessionHistory() {
        val ctx = context ?: return
        val fileList = (sessionDir.listFiles() ?: emptyArray())
            .filter { it.extension == "json" }
            .sortedByDescending { it.lastModified() }.take(50)
            .mapNotNull { parseSessionFile(it) }
            .toMutableList()
        if (fileList.isEmpty()) {
            Toast.makeText(ctx, "No saved sessions", Toast.LENGTH_SHORT).show()
            return
        }
        val labels = fileList.map { "${it.title} (${it.msgCount} msgs)" }.toTypedArray()
        val dialog = android.app.AlertDialog.Builder(ctx)
            .setTitle("History (${fileList.size})")
            .setItems(labels) { dlg, which ->
                loadSession(fileList[which].file)
                (dlg as android.app.AlertDialog).dismiss()
            }
            .setNegativeButton("Close", null)
            .create()

        dialog.setOnShowListener {
            dialog.listView?.setOnItemLongClickListener { _, _, position, _ ->
                val session = fileList[position]
                android.app.AlertDialog.Builder(ctx)
                    .setTitle("Delete Session")
                    .setMessage("Delete \"${session.title}\"?")
                    .setPositiveButton("Delete") { _, _ ->
                        session.file.delete()
                        fileList.removeAt(position)
                        if (fileList.isEmpty()) {
                            dialog.dismiss()
                            Toast.makeText(ctx, "No sessions left", Toast.LENGTH_SHORT).show()
                        } else {
                            // Refresh: close and reopen
                            dialog.dismiss()
                            showSessionHistory()
                        }
                    }
                    .setNegativeButton("Cancel", null)
                    .show()
                true
            }
        }
        dialog.show()
    }

    private data class SessionInfo(val file: java.io.File, val title: String, val msgCount: Int)

    private fun parseSessionFile(file: java.io.File): SessionInfo? {
        return try {
            val arr = org.json.JSONArray(file.readText())
            if (arr.length() == 0) return null
            var title = "Untitled"
            var msgCount = 0
            for (i in 0 until arr.length()) {
                val obj = arr.getJSONObject(i)
                if (obj.optString("type") == "message") {
                    msgCount++
                    if (obj.optString("role") == "USER" && title == "Untitled")
                        title = obj.optString("text", "").take(40)
                }
            }
            SessionInfo(file, title, msgCount)
        } catch (_: Exception) { null }
    }

    private fun loadSession(file: java.io.File) {
        try {
            val arr = org.json.JSONArray(file.readText())
            displayMode.clear(); engine?.clearHistory()
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
                        displayMode.addToolCall(ChatItem.ToolCall(
                            toolUseId = "hist-$i", toolName = obj.optString("name"),
                            target = obj.optString("target", "").ifEmpty { null },
                            args = emptyMap(), status = status,
                            durationMs = obj.optLong("duration", 0),
                            result = obj.optString("result", "").ifEmpty { null }
                        ))
                    }
                }
            }
        } catch (e: Exception) {
            Toast.makeText(context, "Load failed: ${e.message}", Toast.LENGTH_SHORT).show()
        }
    }

    // ---- Voice (record + ASR) ----

    private fun startVoiceInput() {
        if (isRecording) { stopRecordingAndTranscribe(); return }
        if (androidx.core.content.ContextCompat.checkSelfPermission(requireContext(),
                android.Manifest.permission.RECORD_AUDIO) != android.content.pm.PackageManager.PERMISSION_GRANTED) {
            permissionLauncher.launch(arrayOf(android.Manifest.permission.RECORD_AUDIO))
            return
        }
        try {
            recordFile = java.io.File(requireContext().cacheDir, "voice_${System.currentTimeMillis()}.m4a")
            mediaRecorder = (if (android.os.Build.VERSION.SDK_INT >= android.os.Build.VERSION_CODES.S)
                android.media.MediaRecorder(requireContext()) else @Suppress("DEPRECATION") android.media.MediaRecorder()
            ).apply {
                setAudioSource(android.media.MediaRecorder.AudioSource.MIC)
                setOutputFormat(android.media.MediaRecorder.OutputFormat.MPEG_4)
                setAudioEncoder(android.media.MediaRecorder.AudioEncoder.AAC)
                setAudioSamplingRate(16000); setAudioChannels(1)
                setOutputFile(recordFile!!.absolutePath)
                prepare(); start()
            }
            isRecording = true
            view?.findViewById<com.google.android.material.button.MaterialButton>(R.id.btnVoice)?.apply {
                setIconResource(R.drawable.ic_send)
            }
            tvStatus.text = "Recording... tap mic to stop"
            tvStatus.visibility = View.VISIBLE
            android.os.Handler(android.os.Looper.getMainLooper()).postDelayed({
                if (isRecording) stopRecordingAndTranscribe()
            }, 30_000)
        } catch (e: Exception) {
            Toast.makeText(context, "Recording failed: ${e.message}", Toast.LENGTH_SHORT).show()
        }
    }

    private fun stopRecordingAndTranscribe() {
        if (!isRecording) return
        isRecording = false
        view?.findViewById<com.google.android.material.button.MaterialButton>(R.id.btnVoice)?.apply {
            setIconResource(R.drawable.ic_mic)
        }
        try { mediaRecorder?.stop(); mediaRecorder?.release() } catch (_: Exception) {}
        mediaRecorder = null
        val audioFile = recordFile ?: return
        if (!audioFile.exists() || audioFile.length() < 1000) {
            Toast.makeText(context, "Recording too short", Toast.LENGTH_SHORT).show()
            audioFile.delete(); return
        }
        tvStatus.text = "Transcribing..."
        tvStatus.visibility = View.VISIBLE
        Thread {
            try {
                val text = transcribeAudio(audioFile)
                audioFile.delete()
                if (!isAdded) return@Thread
                requireActivity().runOnUiThread {
                    tvStatus.visibility = View.GONE
                    if (text.isNotBlank()) { etInput.setText(text); sendMessage(text) }
                    else Toast.makeText(context, "No speech detected", Toast.LENGTH_SHORT).show()
                }
            } catch (e: Exception) {
                audioFile.delete()
                if (!isAdded) return@Thread
                requireActivity().runOnUiThread {
                    tvStatus.visibility = View.GONE
                    Toast.makeText(context, "Transcription failed: ${e.message}", Toast.LENGTH_SHORT).show()
                }
            }
        }.start()
    }

    private fun transcribeAudio(file: java.io.File): String {
        val useProxy = config.proxyUrl.isNotEmpty()
        val baseUrl: String; val apiKey: String; val model: String
        if (useProxy) {
            baseUrl = config.proxyUrl.trimEnd('/'); apiKey = ""; model = "FunAudioLLM/SenseVoiceSmall"
        } else {
            apiKey = config.aiApiKey
            baseUrl = when (config.aiProvider) {
                "siliconflow" -> "https://api.siliconflow.cn/v1"
                "openai" -> config.aiBaseUrl.ifEmpty { "https://api.openai.com/v1" }
                else -> config.aiBaseUrl.ifEmpty { "https://api.siliconflow.cn/v1" }
            }.trimEnd('/')
            model = when (config.aiProvider) {
                "siliconflow" -> "FunAudioLLM/SenseVoiceSmall"; "openai" -> "whisper-1"
                else -> "FunAudioLLM/SenseVoiceSmall"
            }
        }
        val boundary = "----OctoVoice${System.currentTimeMillis()}"
        val crlf = "\r\n"
        val bodyStream = java.io.ByteArrayOutputStream()
        val writer = java.io.OutputStreamWriter(bodyStream)
        writer.append("--$boundary$crlf")
        writer.append("Content-Disposition: form-data; name=\"model\"$crlf$crlf")
        writer.append("$model$crlf")
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
        conn.requestMethod = "POST"; conn.doOutput = true
        conn.connectTimeout = 15000; conn.readTimeout = 30000
        if (apiKey.isNotEmpty()) conn.setRequestProperty("Authorization", "Bearer $apiKey")
        conn.setRequestProperty("Content-Type", "multipart/form-data; boundary=$boundary")
        conn.outputStream.use { it.write(body) }
        val respCode = conn.responseCode
        val respBody = (if (respCode in 200..299) conn.inputStream else conn.errorStream).bufferedReader().readText()
        if (respCode !in 200..299) throw Exception("ASR API error $respCode: $respBody")
        return org.json.JSONObject(respBody).optString("text", "").trim()
    }
}
