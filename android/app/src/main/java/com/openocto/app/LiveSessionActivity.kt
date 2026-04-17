package com.openocto.app

import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.view.View
import android.view.inputmethod.EditorInfo
import android.widget.EditText
import android.widget.ScrollView
import android.widget.TextView
import android.widget.Toast
import androidx.appcompat.app.AppCompatActivity

/**
 * Live view of a remote `octo agent` session.
 * Shows terminal output in real-time, allows sending input from phone.
 */
class LiveSessionActivity : AppCompatActivity() {

    companion object {
        const val EXTRA_SESSION_NAME = "session_name"
        private const val POLL_INTERVAL = 2000L
    }

    private lateinit var config: OctoConfig
    private var relay: RelayClient? = null
    private var sessionName = ""

    private lateinit var tvOutput: TextView
    private lateinit var scrollOutput: ScrollView
    private lateinit var tvSessionStatus: TextView
    private lateinit var tvLatency: TextView
    private lateinit var statusDot: View
    private lateinit var etInput: EditText

    private val handler = Handler(Looper.getMainLooper())
    private var pollRunnable: Runnable? = null
    private var lastContent = ""
    private var lastPollTime = 0L

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_live_session)

        config = OctoConfig(this)
        sessionName = intent.getStringExtra(EXTRA_SESSION_NAME) ?: ""

        if (sessionName.isEmpty()) {
            Toast.makeText(this, "No session name", Toast.LENGTH_SHORT).show()
            finish()
            return
        }

        // Setup relay
        relay = RelayClient(config.redisUrl, config.redisToken, config.workspace,
            proxyUrl = config.proxyUrl)

        // Toolbar
        val toolbar = findViewById<com.google.android.material.appbar.MaterialToolbar>(R.id.toolbar)
        toolbar.title = "  $sessionName"
        toolbar.subtitle = "  ${getString(R.string.live_session)}"
        toolbar.setNavigationOnClickListener { finish() }

        // Views
        tvOutput = findViewById(R.id.tvOutput)
        scrollOutput = findViewById(R.id.scrollOutput)
        tvSessionStatus = findViewById(R.id.tvSessionStatus)
        tvLatency = findViewById(R.id.tvLatency)
        statusDot = findViewById(R.id.statusDot)
        etInput = findViewById(R.id.etInput)

        tvSessionStatus.text = getString(R.string.session_connecting)

        // Send button
        findViewById<com.google.android.material.button.MaterialButton>(R.id.btnSend)
            .setOnClickListener { sendInput() }

        // Enter key sends
        etInput.setOnEditorActionListener { _, actionId, _ ->
            if (actionId == EditorInfo.IME_ACTION_SEND) {
                sendInput()
                true
            } else false
        }
    }

    override fun onResume() {
        super.onResume()
        startPolling()
    }

    override fun onPause() {
        super.onPause()
        stopPolling()
    }

    private fun startPolling() {
        stopPolling()
        pollRunnable = object : Runnable {
            override fun run() {
                fetchSession()
                handler.postDelayed(this, POLL_INTERVAL)
            }
        }
        handler.post(pollRunnable!!)
    }

    private fun stopPolling() {
        pollRunnable?.let { handler.removeCallbacks(it) }
        pollRunnable = null
    }

    private fun fetchSession() {
        val r = relay ?: return
        Thread {
            try {
                val pollStart = System.currentTimeMillis()
                val (output, meta) = r.getSession(sessionName)
                val latency = System.currentTimeMillis() - pollStart

                val status = meta.optString("status", "unknown")
                val isActive = status == "active"

                runOnUiThread {
                    // Update latency
                    tvLatency.text = "${latency}ms"
                    lastPollTime = System.currentTimeMillis()

                    // Update status
                    if (isActive && output.isNotEmpty()) {
                        tvSessionStatus.text = getString(R.string.session_active)
                        statusDot.setBackgroundResource(R.drawable.bg_status_dot_online)
                        etInput.isEnabled = true
                    } else if (status == "ended") {
                        tvSessionStatus.text = getString(R.string.session_ended)
                        statusDot.setBackgroundResource(R.drawable.bg_status_dot_offline)
                        etInput.isEnabled = false
                    } else if (output.isEmpty()) {
                        tvSessionStatus.text = getString(R.string.no_active_session)
                        statusDot.setBackgroundResource(R.drawable.bg_status_dot_offline)
                        etInput.isEnabled = false
                    }

                    // Update terminal content (only if changed)
                    if (output != lastContent && output.isNotEmpty()) {
                        lastContent = output
                        tvOutput.text = output

                        // Auto-scroll to bottom
                        scrollOutput.post {
                            scrollOutput.fullScroll(ScrollView.FOCUS_DOWN)
                        }
                    }
                }
            } catch (e: Exception) {
                runOnUiThread {
                    tvSessionStatus.text = "Error: ${e.message?.take(40)}"
                    statusDot.setBackgroundResource(R.drawable.bg_status_dot_offline)
                }
            }
        }.start()
    }

    private fun sendInput() {
        val text = etInput.text.toString().trim()
        if (text.isEmpty()) return

        val r = relay ?: return
        etInput.text.clear()

        Thread {
            try {
                r.sendSessionInput(sessionName, text)
            } catch (e: Exception) {
                runOnUiThread {
                    Toast.makeText(this, "Send failed: ${e.message}", Toast.LENGTH_SHORT).show()
                }
            }
        }.start()
    }
}
