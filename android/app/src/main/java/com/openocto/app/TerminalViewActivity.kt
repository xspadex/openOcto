package com.openocto.app

import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.view.View
import android.widget.ScrollView
import android.widget.TextView
import androidx.appcompat.app.AppCompatActivity
import androidx.core.content.ContextCompat

/**
 * Read-only view of a remote terminal's current task and output.
 * Polls every 1.5s for live updates.
 */
class TerminalViewActivity : AppCompatActivity() {

    companion object {
        const val EXTRA_TERMINAL_NAME = "terminal_name"
        private const val POLL_FAST = 500L   // while RUNNING
        private const val POLL_SLOW = 2000L  // while idle/done
    }

    private lateinit var config: OctoConfig
    private var relay: RelayClient? = null
    private var terminalName = ""

    private lateinit var tvTaskType: TextView
    private lateinit var tvTaskMeta: TextView
    private lateinit var tvTaskStatus: TextView
    private lateinit var statusDot: View
    private lateinit var tvOutput: TextView
    private lateinit var scrollOutput: ScrollView

    private lateinit var etCommand: android.widget.EditText
    private var fontSize = 10f  // sp
    private val commandHistory = mutableListOf<String>()
    private var historyIndex = -1

    private val handler = Handler(Looper.getMainLooper())
    private var polling = false
    private var lastOutputLen = 0
    private var autoScroll = true
    private var currentPollInterval = POLL_SLOW
    private var isTaskRunning = false

    private val pollRunnable = object : Runnable {
        override fun run() {
            if (!polling) return
            pollTask()
            handler.postDelayed(this, currentPollInterval)
        }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_terminal_view)

        config = OctoConfig(this)
        terminalName = intent.getStringExtra(EXTRA_TERMINAL_NAME) ?: ""

        if (terminalName.isEmpty() || !config.isConfigured) {
            finish()
            return
        }

        relay = RelayClient(
            config.redisUrl, config.redisToken, config.workspace,
            proxyUrl = config.proxyUrl
        )

        // Views
        tvTaskType = findViewById(R.id.tvTaskType)
        tvTaskMeta = findViewById(R.id.tvTaskMeta)
        tvTaskStatus = findViewById(R.id.tvTaskStatus)
        statusDot = findViewById(R.id.statusDot)
        tvOutput = findViewById(R.id.tvOutput)
        scrollOutput = findViewById(R.id.scrollOutput)

        // Toolbar
        findViewById<com.google.android.material.appbar.MaterialToolbar>(R.id.toolbar).apply {
            title = terminalName
            setNavigationOnClickListener { finish() }
        }

        // Command input
        etCommand = findViewById(R.id.etCommand)
        loadCommandHistory()

        findViewById<com.google.android.material.button.MaterialButton>(R.id.btnRun).setOnClickListener {
            val cmd = etCommand.text.toString().trim()
            if (cmd.isNotEmpty() && !isTaskRunning) {
                addToHistory(cmd)
                runCommand(cmd)
            }
        }
        etCommand.setOnEditorActionListener { _, actionId, _ ->
            if (actionId == android.view.inputmethod.EditorInfo.IME_ACTION_SEND) {
                val cmd = etCommand.text.toString().trim()
                if (cmd.isNotEmpty() && !isTaskRunning) {
                    addToHistory(cmd)
                    runCommand(cmd)
                }
                true
            } else false
        }

        // Up/Down key for command history
        etCommand.setOnKeyListener { _, keyCode, event ->
            if (event.action == android.view.KeyEvent.ACTION_DOWN) {
                when (keyCode) {
                    android.view.KeyEvent.KEYCODE_DPAD_UP -> { navigateHistory(-1); true }
                    android.view.KeyEvent.KEYCODE_DPAD_DOWN -> { navigateHistory(1); true }
                    else -> false
                }
            } else false
        }

        // History button (up arrow)
        findViewById<android.view.View>(R.id.btnHistoryUp)?.setOnClickListener { navigateHistory(-1) }
        findViewById<android.view.View>(R.id.btnHistoryDown)?.setOnClickListener { navigateHistory(1) }

        // Pinch to zoom terminal font
        val scaleDetector = android.view.ScaleGestureDetector(this,
            object : android.view.ScaleGestureDetector.SimpleOnScaleGestureListener() {
                override fun onScale(detector: android.view.ScaleGestureDetector): Boolean {
                    fontSize = (fontSize * detector.scaleFactor).coerceIn(6f, 20f)
                    tvOutput.textSize = fontSize
                    return true
                }
            })
        scrollOutput.setOnTouchListener { _, event ->
            scaleDetector.onTouchEvent(event)
            false // don't consume, let scroll work
        }

        // Detect manual scroll → stop auto-scroll
        scrollOutput.viewTreeObserver.addOnScrollChangedListener {
            val maxScroll = tvOutput.height - scrollOutput.height + scrollOutput.paddingTop + scrollOutput.paddingBottom
            autoScroll = scrollOutput.scrollY >= maxScroll - 50
        }

        tvTaskType.text = "Loading..."
        tvTaskMeta.text = ""
        tvOutput.text = ""
    }

    override fun onResume() {
        super.onResume()
        polling = true
        lastOutputLen = 0
        handler.post(pollRunnable)
    }

    override fun onPause() {
        super.onPause()
        polling = false
        handler.removeCallbacks(pollRunnable)
    }

    private fun addToHistory(cmd: String) {
        commandHistory.remove(cmd)
        commandHistory.add(cmd)
        if (commandHistory.size > 50) commandHistory.removeAt(0)
        historyIndex = commandHistory.size
        saveCommandHistory()
    }

    private fun navigateHistory(direction: Int) {
        if (commandHistory.isEmpty()) return
        historyIndex = (historyIndex + direction).coerceIn(0, commandHistory.size)
        if (historyIndex < commandHistory.size) {
            etCommand.setText(commandHistory[historyIndex])
            etCommand.setSelection(etCommand.text.length)
        } else {
            etCommand.setText("")
        }
    }

    private fun loadCommandHistory() {
        val prefs = getSharedPreferences("terminal_history", MODE_PRIVATE)
        val json = prefs.getString("history_$terminalName", null) ?: return
        try {
            val arr = org.json.JSONArray(json)
            for (i in 0 until arr.length()) commandHistory.add(arr.getString(i))
            historyIndex = commandHistory.size
        } catch (_: Exception) {}
    }

    private fun saveCommandHistory() {
        val prefs = getSharedPreferences("terminal_history", MODE_PRIVATE)
        val arr = org.json.JSONArray(commandHistory)
        prefs.edit().putString("history_$terminalName", arr.toString()).apply()
    }

    private fun runCommand(cmd: String) {
        etCommand.setText("")
        tvOutput.text = ""
        lastOutputLen = 0
        isTaskRunning = true
        currentPollInterval = POLL_FAST

        Thread {
            try {
                relay?.submitTask(terminalName, "shell", mapOf("command" to cmd))
            } catch (e: Exception) {
                runOnUiThread {
                    tvOutput.text = "Error: ${e.message}"
                    isTaskRunning = false
                    currentPollInterval = POLL_SLOW
                }
            }
        }.start()
    }

    private fun pollTask() {
        Thread {
            try {
                val task = relay?.pollTask(terminalName)

                // Also get terminal info for context
                val terminals = relay?.listTerminals() ?: emptyList()
                val termInfo = terminals.firstOrNull { it.optString("name") == terminalName }
                val online = termInfo?.optBoolean("online") ?: false
                val meta = termInfo?.optJSONObject("meta")
                val platform = meta?.optString("platform", "") ?: ""
                val shell = meta?.optString("shell", "") ?: ""

                runOnUiThread {
                    // Status dot
                    statusDot.setBackgroundResource(
                        if (online) R.drawable.bg_status_dot_online
                        else R.drawable.bg_status_dot_offline
                    )

                    if (task == null || task.optString("status").isEmpty()) {
                        tvTaskType.text = "Idle"
                        tvTaskMeta.text = listOfNotNull(
                            platform.ifEmpty { null },
                            shell.ifEmpty { null }
                        ).joinToString(" | ").ifEmpty { "No active task" }
                        tvTaskStatus.text = "IDLE"
                        tvTaskStatus.setBackgroundResource(0)
                        tvTaskStatus.setTextColor(
                            ContextCompat.getColor(this, R.color.status_offline)
                        )
                        if (tvOutput.text.isEmpty()) {
                            tvOutput.text = "No task running on this terminal."
                        }
                        return@runOnUiThread
                    }

                    val type = task.optString("type", "?")
                    val status = task.optString("status", "?")
                    val output = task.optString("output", "")
                    val exitCode = task.optInt("exit_code", -1)
                    val elapsed = (System.currentTimeMillis() / 1000) - task.optLong("created_at", 0)

                    // Type + command
                    val cmd = when (type) {
                        "shell" -> task.optString("command", "")
                        "cat" -> "cat ${task.optString("path", "")}"
                        "edit" -> "edit ${task.optString("path", "")}"
                        "glob" -> "glob ${task.optString("pattern", "")}"
                        "grep" -> "grep ${task.optString("pattern", "")}"
                        "ai_request" -> "ai: ${task.optString("prompt", "").take(60)}"
                        else -> type
                    }
                    tvTaskType.text = cmd.ifEmpty { type }

                    // Meta line
                    val elapsedStr = if (elapsed < 60) "${elapsed}s"
                        else "${elapsed / 60}m ${elapsed % 60}s"
                    tvTaskMeta.text = "$type | $elapsedStr" +
                        if (status == "DONE" || status == "FAILED") " | exit $exitCode" else ""

                    // Adjust polling speed
                    isTaskRunning = status in listOf("RUNNING", "PENDING")
                    currentPollInterval = if (isTaskRunning) POLL_FAST else POLL_SLOW

                    // Status badge
                    tvTaskStatus.text = status
                    when (status) {
                        "RUNNING" -> {
                            tvTaskStatus.setBackgroundResource(R.drawable.bg_status_running)
                            tvTaskStatus.setTextColor(
                                ContextCompat.getColor(this, R.color.status_online)
                            )
                        }
                        "PENDING" -> {
                            tvTaskStatus.setBackgroundResource(R.drawable.bg_status_pending)
                            tvTaskStatus.setTextColor(0xFFFF9500.toInt())
                        }
                        "DONE" -> {
                            tvTaskStatus.setBackgroundResource(R.drawable.bg_status_running)
                            tvTaskStatus.setTextColor(
                                ContextCompat.getColor(this, R.color.status_online)
                            )
                        }
                        "FAILED" -> {
                            tvTaskStatus.setBackgroundResource(0)
                            tvTaskStatus.setTextColor(
                                ContextCompat.getColor(this, R.color.status_error)
                            )
                        }
                        else -> {
                            tvTaskStatus.setBackgroundResource(0)
                            tvTaskStatus.setTextColor(
                                ContextCompat.getColor(this, R.color.status_offline)
                            )
                        }
                    }

                    // Output (incremental append for performance)
                    if (output.length > lastOutputLen) {
                        tvOutput.append(output.substring(lastOutputLen))
                        lastOutputLen = output.length
                        if (autoScroll) {
                            scrollOutput.post {
                                scrollOutput.fullScroll(ScrollView.FOCUS_DOWN)
                            }
                        }
                    } else if (output.length < lastOutputLen) {
                        // New task or cleared — reset
                        tvOutput.text = output
                        lastOutputLen = output.length
                    }
                }
            } catch (e: Exception) {
                runOnUiThread {
                    tvTaskMeta.text = "Error: ${e.message}"
                }
            }
        }.start()
    }
}
