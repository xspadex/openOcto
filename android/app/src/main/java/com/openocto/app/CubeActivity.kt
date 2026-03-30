package com.openocto.app

import android.animation.ValueAnimator
import android.app.Activity
import android.app.AlertDialog
import android.content.Intent
import android.graphics.Typeface
import android.os.Bundle
import android.speech.RecognizerIntent
import android.view.Gravity
import android.view.HapticFeedbackConstants
import android.view.inputmethod.EditorInfo
import android.widget.*
import androidx.activity.result.contract.ActivityResultContracts
import androidx.appcompat.app.AppCompatActivity
import androidx.core.content.ContextCompat
import androidx.core.view.ViewCompat
import androidx.core.view.WindowInsetsCompat
import com.google.android.material.chip.Chip
import com.google.android.material.chip.ChipGroup
import java.util.Locale

/**
 * AI Cube — unified AI entry point that delegates to a remote agent.
 *
 * Architecture: Phone (Client) -> Remote Agent (Mac with claude/API) -> Terminals
 */
class CubeActivity : AppCompatActivity() {

    private lateinit var config: OctoConfig
    private var relay: RelayClient? = null

    private lateinit var chatContainer: LinearLayout
    private lateinit var scrollChat: ScrollView
    private lateinit var etInput: EditText
    private lateinit var tvStatus: TextView
    private lateinit var chipDevices: ChipGroup
    private lateinit var deviceBar: HorizontalScrollView

    private var agentName: String = ""
    private var typingAnimator: ValueAnimator? = null

    private val speechLauncher = registerForActivityResult(
        ActivityResultContracts.StartActivityForResult()
    ) { result ->
        if (result.resultCode == Activity.RESULT_OK) {
            val text = result.data
                ?.getStringArrayListExtra(RecognizerIntent.EXTRA_RESULTS)
                ?.firstOrNull()
            if (!text.isNullOrBlank()) {
                etInput.setText(text)
                sendMessage(text)
            }
        }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_cube)

        config = OctoConfig(this)

        chatContainer = findViewById(R.id.chatContainer)
        scrollChat = findViewById(R.id.scrollChat)
        etInput = findViewById(R.id.etInput)
        tvStatus = findViewById(R.id.tvStatus)
        chipDevices = findViewById(R.id.chipDevices)
        deviceBar = findViewById(R.id.deviceBar)

        // Toolbar
        findViewById<com.google.android.material.appbar.MaterialToolbar>(R.id.toolbar).apply {
            setNavigationOnClickListener { finish() }
            setOnMenuItemClickListener { item ->
                if (item.itemId == R.id.menu_settings) {
                    showAgentSettings()
                    true
                } else false
            }
            inflateMenu(R.menu.cube_menu)
        }

        // Send
        findViewById<com.google.android.material.button.MaterialButton>(R.id.btnSend)
            .setOnClickListener { v ->
                val text = etInput.text.toString().trim()
                if (text.isNotEmpty()) {
                    v.performHapticFeedback(HapticFeedbackConstants.CONFIRM)
                    sendMessage(text)
                }
            }

        // Voice
        findViewById<com.google.android.material.button.MaterialButton>(R.id.btnVoice)
            .setOnClickListener { startVoiceInput() }

        // Wake
        findViewById<com.google.android.material.button.MaterialButton>(R.id.btnWake)
            .setOnClickListener { showWakeDialog() }

        // Enter = send
        etInput.setOnEditorActionListener { _, actionId, _ ->
            if (actionId == EditorInfo.IME_ACTION_SEND) {
                val text = etInput.text.toString().trim()
                if (text.isNotEmpty()) sendMessage(text)
                true
            } else false
        }

        // Handle system insets: top (status bar / dynamic island) + bottom (keyboard / nav bar)
        val rootView = findViewById<android.view.View>(android.R.id.content)?.let {
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
                    scrollChat.post { scrollChat.fullScroll(ScrollView.FOCUS_DOWN) }
                }
                WindowInsetsCompat.CONSUMED
            }
        }

        // Init relay
        if (config.isConfigured) {
            relay = RelayClient(
                config.redisUrl, config.redisToken, config.workspace,
                proxyUrl = config.proxyUrl
            )
        }

        loadDevices()
        addBubble("Hi! I'm your AI Cube. Ask me anything or tell me to do something on your devices.", false)
    }

    override fun onDestroy() {
        typingAnimator?.cancel()
        super.onDestroy()
    }

    // ---- Chat ----

    private fun sendMessage(text: String) {
        etInput.setText("")
        addBubble(text, true)

        if (!config.isConfigured || relay == null) {
            addBubble("octo relay not configured. Go back and scan QR code first.", false)
            return
        }

        val agent = resolveAgent()
        if (agent == null) {
            addBubble("No AI agent online. Start one with:\n  octo agent-serve", false)
            return
        }

        setStatus("Sending to $agent")
        startTypingAnimation()

        Thread {
            try {
                relay!!.submitTask(agent, "ai_request", mapOf(
                    "prompt" to text,
                    "sender" to config.terminalName
                ))

                var elapsed = 0
                val timeout = 120
                while (elapsed < timeout) {
                    Thread.sleep(2000)
                    elapsed += 2

                    val task = relay!!.pollTask(agent) ?: break
                    val status = task.optString("status")
                    val output = task.optString("output", "")

                    if (status == "RUNNING") {
                        val statusText = if (output.isNotEmpty()) {
                            "Processing... (${output.length} chars)"
                        } else "Thinking"
                        runOnUiThread { setStatus(statusText) }
                    }

                    if (status in listOf("DONE", "FAILED")) {
                        relay!!.clearTask(agent)
                        val exitCode = task.optInt("exit_code", -1)
                        runOnUiThread {
                            stopTypingAnimation()
                            setStatus(null)
                            if (exitCode == 0 && output.isNotEmpty()) {
                                addBubble(output, false)
                            } else if (output.isNotEmpty()) {
                                addBubble("Error: $output", false)
                            } else {
                                addBubble("Agent returned no response.", false)
                            }
                        }
                        return@Thread
                    }
                }

                runOnUiThread {
                    stopTypingAnimation()
                    setStatus(null)
                    addBubble("Timed out waiting for agent response.", false)
                }
            } catch (e: Exception) {
                runOnUiThread {
                    stopTypingAnimation()
                    setStatus(null)
                    addBubble("Error: ${e.message}", false)
                }
            }
        }.start()
    }

    private fun resolveAgent(): String? {
        if (agentName.isNotEmpty()) return agentName
        try {
            val terminals = relay?.listTerminals() ?: return null
            val agents = terminals.filter { t ->
                t.optBoolean("online") &&
                t.optJSONObject("meta")?.optBoolean("ai_agent", false) == true
            }
            if (agents.isEmpty()) return null
            val preferred = config.preferredAgent
            if (preferred.isNotEmpty()) {
                val match = agents.find { it.optString("name") == preferred }
                if (match != null) {
                    agentName = preferred
                    return agentName
                }
            }
            agentName = agents.first().optString("name")
            return agentName
        } catch (_: Exception) {
            return null
        }
    }

    private fun addBubble(text: String, isUser: Boolean) {
        val tv = TextView(this).apply {
            this.text = text
            textSize = 15f
            setPadding(36, 24, 36, 24)
            val params = LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.WRAP_CONTENT,
                LinearLayout.LayoutParams.WRAP_CONTENT
            ).apply {
                gravity = if (isUser) Gravity.END else Gravity.START
                setMargins(
                    if (isUser) 64 else 0, 6,
                    if (isUser) 0 else 64, 6
                )
            }
            layoutParams = params

            if (isUser) {
                setBackgroundResource(R.drawable.bg_bubble_user)
                setTextColor(ContextCompat.getColor(context, R.color.bubble_user_text))
            } else {
                setBackgroundResource(R.drawable.bg_bubble_ai)
                setTextColor(ContextCompat.getColor(context, R.color.bubble_ai_text))
            }
        }
        chatContainer.addView(tv)
        scrollChat.post { scrollChat.fullScroll(ScrollView.FOCUS_DOWN) }
    }

    private fun setStatus(text: String?) {
        tvStatus.text = text ?: ""
        tvStatus.visibility = if (text != null) TextView.VISIBLE else TextView.GONE
    }

    private fun startTypingAnimation() {
        typingAnimator?.cancel()
        var dots = 0
        typingAnimator = ValueAnimator.ofInt(0, 3).apply {
            duration = 1200
            repeatCount = ValueAnimator.INFINITE
            addUpdateListener {
                dots = (dots + 1) % 4
                val base = tvStatus.text.toString().replace(Regex("\\.+$"), "")
                tvStatus.text = base + ".".repeat(dots)
            }
            start()
        }
    }

    private fun stopTypingAnimation() {
        typingAnimator?.cancel()
        typingAnimator = null
    }

    // ---- Voice ----

    private fun startVoiceInput() {
        val intent = Intent(RecognizerIntent.ACTION_RECOGNIZE_SPEECH).apply {
            putExtra(RecognizerIntent.EXTRA_LANGUAGE_MODEL, RecognizerIntent.LANGUAGE_MODEL_FREE_FORM)
            putExtra(RecognizerIntent.EXTRA_LANGUAGE, Locale.getDefault())
            putExtra(RecognizerIntent.EXTRA_PROMPT, "Say something...")
        }
        try {
            speechLauncher.launch(intent)
        } catch (_: Exception) {
            Toast.makeText(this, "Speech recognition not available", Toast.LENGTH_SHORT).show()
        }
    }

    // ---- Wake ----

    private fun showWakeDialog() {
        if (relay == null) {
            Toast.makeText(this, "Not configured", Toast.LENGTH_SHORT).show()
            return
        }
        Thread {
            try {
                val terminals = relay!!.listTerminals()
                    .filter { it.optBoolean("online") && it.optString("name") != config.terminalName }
                    .map { it.optString("name") }
                runOnUiThread {
                    if (terminals.isEmpty()) {
                        Toast.makeText(this, "No other online devices", Toast.LENGTH_SHORT).show()
                        return@runOnUiThread
                    }
                    AlertDialog.Builder(this)
                        .setTitle("Wake device")
                        .setItems(terminals.toTypedArray()) { _, idx ->
                            wakeDevice(terminals[idx])
                        }
                        .show()
                }
            } catch (e: Exception) {
                runOnUiThread {
                    Toast.makeText(this, "Error: ${e.message}", Toast.LENGTH_SHORT).show()
                }
            }
        }.start()
    }

    private fun wakeDevice(target: String) {
        Thread {
            try {
                relay!!.submitTask(target, "wake_input", emptyMap())
                runOnUiThread { addBubble("Wake signal sent to $target", false) }
            } catch (e: Exception) {
                runOnUiThread { addBubble("Failed to wake $target: ${e.message}", false) }
            }
        }.start()
    }

    // ---- Device Chips ----

    private fun loadDevices() {
        if (relay == null) return
        Thread {
            try {
                val terminals = relay!!.listTerminals()
                runOnUiThread {
                    chipDevices.removeAllViews()
                    for (t in terminals) {
                        val name = t.optString("name")
                        val online = t.optBoolean("online")
                        val isAgent = t.optJSONObject("meta")?.optBoolean("ai_agent", false) == true
                        val chip = Chip(this).apply {
                            text = if (isAgent) "$name (agent)" else name
                            isCheckable = false
                            isEnabled = online
                            alpha = if (online) 1f else 0.4f
                            setOnClickListener {
                                if (isAgent) {
                                    agentName = name
                                    config.preferredAgent = name
                                    Toast.makeText(this@CubeActivity, "Using agent: $name", Toast.LENGTH_SHORT).show()
                                } else {
                                    etInput.setText("@$name ")
                                    etInput.setSelection(etInput.text.length)
                                    etInput.requestFocus()
                                }
                            }
                        }
                        chipDevices.addView(chip)
                    }
                    deviceBar.visibility = if (terminals.isNotEmpty()) HorizontalScrollView.VISIBLE
                        else HorizontalScrollView.GONE
                }
            } catch (_: Exception) {}
        }.start()
    }

    // ---- Settings ----

    private fun showAgentSettings() {
        if (relay == null) {
            Toast.makeText(this, "Not configured", Toast.LENGTH_SHORT).show()
            return
        }

        Thread {
            val agents = try {
                relay!!.listTerminals()
                    .filter { it.optJSONObject("meta")?.optBoolean("ai_agent", false) == true }
                    .map { t ->
                        val name = t.optString("name")
                        val online = t.optBoolean("online")
                        val label = t.optJSONObject("meta")?.optString("ai_label", "") ?: ""
                        Triple(name, online, label)
                    }
            } catch (_: Exception) { emptyList() }

            runOnUiThread {
                val layout = LinearLayout(this).apply {
                    orientation = LinearLayout.VERTICAL
                    setPadding(48, 32, 48, 16)
                }

                val agentLabel = TextView(this).apply {
                    text = "Select AI Agent"
                    textSize = 16f
                    setPadding(0, 0, 0, 16)
                }
                layout.addView(agentLabel)

                if (agents.isEmpty()) {
                    val noAgent = TextView(this).apply {
                        text = "No agents found.\n\nStart one on your Mac/PC:\n  octo agent-serve"
                        textSize = 14f
                        setPadding(0, 0, 0, 16)
                    }
                    layout.addView(noAgent)
                    AlertDialog.Builder(this)
                        .setTitle("AI Cube Settings")
                        .setView(layout)
                        .setPositiveButton("OK", null)
                        .show()
                } else {
                    val currentPref = config.preferredAgent
                    val names = agents.map { (name, online, label) ->
                        val status = if (online) "online" else "offline"
                        "$name ($status) $label"
                    }.toTypedArray()
                    val currentIdx = agents.indexOfFirst { it.first == currentPref }.coerceAtLeast(0)

                    val spinner = Spinner(this)
                    spinner.adapter = ArrayAdapter(this, android.R.layout.simple_spinner_dropdown_item, names)
                    spinner.setSelection(currentIdx)
                    layout.addView(spinner)

                    AlertDialog.Builder(this)
                        .setTitle("AI Cube Settings")
                        .setView(layout)
                        .setPositiveButton("Save") { _, _ ->
                            val idx = spinner.selectedItemPosition
                            if (idx in agents.indices) {
                                val selected = agents[idx].first
                                config.preferredAgent = selected
                                agentName = selected
                                Toast.makeText(this, "Agent: $selected", Toast.LENGTH_SHORT).show()
                            }
                        }
                        .setNegativeButton("Cancel", null)
                        .show()
                }
            }
        }.start()
    }
}
