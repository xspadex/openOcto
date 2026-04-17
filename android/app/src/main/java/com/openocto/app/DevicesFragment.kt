package com.openocto.app

import android.content.Intent
import android.content.res.ColorStateList
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.view.LayoutInflater
import android.view.View
import android.view.ViewGroup
import android.widget.ImageView
import android.widget.LinearLayout
import android.widget.ProgressBar
import android.widget.TextView
import android.widget.Toast
import androidx.core.content.ContextCompat
import androidx.fragment.app.Fragment
import androidx.swiperefreshlayout.widget.SwipeRefreshLayout

/**
 * Devices tab — shows network terminals and local daemon status.
 * Works without relay (shows empty state with scan prompt).
 */
class DevicesFragment : Fragment() {

    companion object {
        // In-memory cache — survives tab switches, cleared on app kill
        private var cachedTerminals: List<org.json.JSONObject>? = null
        private var cachedFeed: List<org.json.JSONObject>? = null
        private var cachedGpuData: MutableMap<String, String> = mutableMapOf()
    }

    private lateinit var config: OctoConfig
    private var relay: RelayClient? = null

    private lateinit var swipeRefresh: SwipeRefreshLayout
    private lateinit var tvPhoneStatus: TextView
    private lateinit var phoneDot: View
    private lateinit var btnJoinLeave: com.google.android.material.button.MaterialButton
    private lateinit var layoutNoRelay: View
    private lateinit var tvTerminalsHeader: TextView
    private lateinit var terminalListContainer: LinearLayout
    private lateinit var cardFeed: View
    private lateinit var feedListContainer: LinearLayout

    // Live sessions
    private lateinit var tvSessionsHeader: TextView
    private lateinit var sessionListContainer: LinearLayout

    // GPU metrics
    private lateinit var tvGpuHeader: TextView
    private lateinit var gpuCardContainer: LinearLayout
    private lateinit var gpuControlRow: View
    private var gpuRefreshInterval = 0L  // 0 = off
    private var gpuRunnable: Runnable? = null
    private var gpuTerminals: List<String> = emptyList()

    private val handler = Handler(Looper.getMainLooper())
    private var pollRunnable: Runnable? = null

    override fun onCreateView(inflater: LayoutInflater, container: ViewGroup?, savedInstanceState: Bundle?): View {
        return inflater.inflate(R.layout.fragment_devices, container, false)
    }

    override fun onViewCreated(view: View, savedInstanceState: Bundle?) {
        super.onViewCreated(view, savedInstanceState)
        config = OctoConfig(requireContext())

        swipeRefresh = view.findViewById(R.id.swipeRefresh)
        tvPhoneStatus = view.findViewById(R.id.tvPhoneStatus)
        phoneDot = view.findViewById(R.id.phoneDot)
        btnJoinLeave = view.findViewById(R.id.btnJoinLeave)
        layoutNoRelay = view.findViewById(R.id.layoutNoRelay)
        tvTerminalsHeader = view.findViewById(R.id.tvTerminalsHeader)
        terminalListContainer = view.findViewById(R.id.terminalListContainer)
        cardFeed = view.findViewById(R.id.cardFeed)
        feedListContainer = view.findViewById(R.id.feedListContainer)
        tvSessionsHeader = view.findViewById(R.id.tvSessionsHeader)
        sessionListContainer = view.findViewById(R.id.sessionListContainer)
        tvGpuHeader = view.findViewById(R.id.tvGpuHeader)
        gpuCardContainer = view.findViewById(R.id.gpuCardContainer)
        gpuControlRow = view.findViewById(R.id.gpuControlRow)

        // GPU refresh interval buttons
        setupGpuIntervalButtons(view)

        btnJoinLeave.setOnClickListener {
            if (DaemonService.isRunning) {
                stopDaemon()
            } else {
                startDaemon()
            }
        }

        swipeRefresh.setOnRefreshListener { loadData() }
        swipeRefresh.setColorSchemeResources(R.color.primary)

        updateUI()
    }

    override fun onResume() {
        super.onResume()
        updateUI()
        startPolling()
    }

    override fun onPause() {
        super.onPause()
        stopPolling()
        stopGpuPolling()
    }

    private fun updateUI() {
        config = OctoConfig(requireContext())
        val isRunning = DaemonService.isRunning
        val hasRelay = config.isConfigured

        // Phone status
        if (isRunning) {
            tvPhoneStatus.text = getString(R.string.online_as, config.terminalName)
            phoneDot.setBackgroundResource(R.drawable.bg_status_dot_online)
            btnJoinLeave.text = getString(R.string.leave_network)
            btnJoinLeave.backgroundTintList = android.content.res.ColorStateList.valueOf(
                ContextCompat.getColor(requireContext(), R.color.status_error))
            btnJoinLeave.setTextColor(0xFFFFFFFF.toInt())
            btnJoinLeave.setOnClickListener { stopDaemon() }
        } else if (hasRelay) {
            tvPhoneStatus.text = getString(R.string.not_joined, config.terminalName.ifEmpty { android.os.Build.MODEL })
            phoneDot.setBackgroundResource(R.drawable.bg_status_dot_offline)
            btnJoinLeave.text = getString(R.string.join_network)
            btnJoinLeave.backgroundTintList = android.content.res.ColorStateList.valueOf(
                ContextCompat.getColor(requireContext(), R.color.primary))
            btnJoinLeave.setTextColor(ContextCompat.getColor(requireContext(), R.color.on_primary))
            btnJoinLeave.setOnClickListener { startDaemon() }
        } else {
            tvPhoneStatus.text = android.os.Build.MODEL
            phoneDot.setBackgroundResource(R.drawable.bg_status_dot_offline)
            btnJoinLeave.text = getString(R.string.scan_qr_to_connect)
            btnJoinLeave.backgroundTintList = android.content.res.ColorStateList.valueOf(
                ContextCompat.getColor(requireContext(), R.color.primary))
            btnJoinLeave.setTextColor(ContextCompat.getColor(requireContext(), R.color.on_primary))
            btnJoinLeave.setOnClickListener { (activity as? MainActivity)?.scanQr() }
        }

        // Show/hide sections
        layoutNoRelay.visibility = if (!hasRelay) View.VISIBLE else View.GONE
        tvTerminalsHeader.visibility = if (hasRelay) View.VISIBLE else View.GONE

        if (hasRelay) {
            relay = RelayClient(config.redisUrl, config.redisToken, config.workspace,
                proxyUrl = config.proxyUrl)
            // Show cached data immediately (zero wait)
            cachedTerminals?.let { buildTerminalList(it) }
            cachedFeed?.let { buildFeed(it) }
            // Then refresh in background
            loadData()
        }
    }

    private fun startDaemon() {
        config = OctoConfig(requireContext())
        if (!config.isConfigured) {
            Toast.makeText(context, "Scan QR code first", Toast.LENGTH_SHORT).show()
            return
        }
        // Auto-generate terminal name if empty
        if (config.terminalName.isEmpty()) {
            config.terminalName = android.os.Build.MODEL
                .lowercase().replace(" ", "-").take(20)
        }
        val intent = Intent(requireContext(), DaemonService::class.java).apply {
            action = DaemonService.ACTION_START
        }
        requireContext().startForegroundService(intent)
        handler.postDelayed({ updateUI(); loadData() }, 2000)
    }

    private fun stopDaemon() {
        val intent = Intent(requireContext(), DaemonService::class.java).apply {
            action = DaemonService.ACTION_STOP
        }
        requireContext().startService(intent)
        handler.postDelayed({ updateUI() }, 1000)
    }

    private fun startPolling() {
        if (!config.isConfigured) return
        stopPolling()
        pollRunnable = object : Runnable {
            override fun run() {
                loadData()
                handler.postDelayed(this, 5000)
            }
        }
        handler.post(pollRunnable!!)
    }

    private fun stopPolling() {
        pollRunnable?.let { handler.removeCallbacks(it) }
        pollRunnable = null
    }

    private fun loadData() {
        val r = relay ?: return
        Thread {
            try {
                val terminals = r.listTerminals()
                val feed = r.getFeed(10)

                // Update cache
                cachedTerminals = terminals
                cachedFeed = feed

                // Identify GPU terminals
                val gpuNames = terminals.filter { t ->
                    val tags = mutableListOf<String>()
                    val tagsArr = t.optJSONArray("tags")
                    if (tagsArr != null) {
                        for (i in 0 until tagsArr.length()) tags.add(tagsArr.optString(i, ""))
                    }
                    t.optBoolean("online", false) &&
                        tags.any { it.contains("gpu", true) || it.contains("cuda", true) }
                }.map { it.optString("name", "") }.filter { it.isNotEmpty() }
                gpuTerminals = gpuNames

                // Discover live sessions (from global hash, not per-terminal)
                val activeSessions = try {
                    r.getActiveSessions().map { it.first }
                } catch (_: Exception) { emptyList() }

                if (!isAdded) return@Thread
                requireActivity().runOnUiThread {
                    swipeRefresh.isRefreshing = false
                    buildTerminalList(terminals)
                    buildSessionList(activeSessions)
                    buildFeed(feed)
                    // Show GPU section if we have GPU terminals
                    if (gpuNames.isNotEmpty()) {
                        tvGpuHeader.visibility = View.VISIBLE
                        gpuControlRow.visibility = View.VISIBLE
                        buildGpuCards()  // Show cached data
                        // Auto-fetch on first discovery
                        if (cachedGpuData.isEmpty()) {
                            fetchGpuMetrics()
                        }
                    } else {
                        tvGpuHeader.visibility = View.GONE
                        gpuControlRow.visibility = View.GONE
                    }
                }
            } catch (e: Exception) {
                if (!isAdded) return@Thread
                requireActivity().runOnUiThread {
                    swipeRefresh.isRefreshing = false
                }
            }
        }.start()
    }

    private fun buildTerminalList(terminals: List<org.json.JSONObject>) {
        terminalListContainer.removeAllViews()
        if (terminals.isEmpty()) {
            tvTerminalsHeader.visibility = View.GONE
            return
        }
        tvTerminalsHeader.visibility = View.VISIBLE

        for (t in terminals) {
            val name = t.optString("name", "?")
            val online = t.optBoolean("online", false)
            val meta = t.optJSONObject("meta")
            val platform = meta?.optString("platform", "") ?: ""

            val view = LayoutInflater.from(context).inflate(R.layout.item_terminal, terminalListContainer, false)
            view.findViewById<TextView>(R.id.tvName).text = name
            val detail = listOfNotNull(
                platform.ifEmpty { null },
                (meta?.optString("shell", "") ?: "").ifEmpty { null }
            ).joinToString(" | ")
            view.findViewById<TextView>(R.id.tvDetail).text = detail.ifEmpty { "\u2014" }
            view.findViewById<View>(R.id.statusDot).setBackgroundResource(
                if (online) R.drawable.bg_status_dot_online else R.drawable.bg_status_dot_offline
            )
            view.findViewById<ImageView>(R.id.ivPlatform).setImageResource(
                if (platform == "android") R.drawable.ic_phone else R.drawable.ic_computer
            )
            if (online) {
                view.setOnClickListener {
                    // Check if this terminal has a live agent session
                    Thread {
                        val hasSession = try {
                            val (output, meta) = relay!!.getSession(name)
                            meta.optString("status") == "active" && output.isNotEmpty()
                        } catch (_: Exception) { false }

                        if (isAdded) requireActivity().runOnUiThread {
                            if (hasSession) {
                                // Show chooser: terminal view or live session
                                android.app.AlertDialog.Builder(requireContext())
                                    .setTitle(name)
                                    .setItems(arrayOf(
                                        "\uD83D\uDCBB ${getString(R.string.live_session)}",
                                        "\uD83D\uDD27 Terminal"
                                    )) { _, which ->
                                        when (which) {
                                            0 -> startActivity(Intent(requireContext(), LiveSessionActivity::class.java).apply {
                                                putExtra(LiveSessionActivity.EXTRA_SESSION_NAME, name)
                                            })
                                            1 -> startActivity(Intent(requireContext(), TerminalViewActivity::class.java).apply {
                                                putExtra(TerminalViewActivity.EXTRA_TERMINAL_NAME, name)
                                            })
                                        }
                                    }
                                    .show()
                            } else {
                                startActivity(Intent(requireContext(), TerminalViewActivity::class.java).apply {
                                    putExtra(TerminalViewActivity.EXTRA_TERMINAL_NAME, name)
                                })
                            }
                        }
                    }.start()
                }
            }
            view.setOnLongClickListener {
                android.app.AlertDialog.Builder(requireContext())
                    .setTitle(getString(R.string.remove_terminal_title))
                    .setMessage(getString(R.string.remove_terminal_msg, name))
                    .setPositiveButton(getString(R.string.remove)) { _, _ ->
                        Thread {
                            try {
                                relay?.unregister(name)
                                if (isAdded) requireActivity().runOnUiThread {
                                    Toast.makeText(context, getString(R.string.removed_toast, name), Toast.LENGTH_SHORT).show()
                                    loadData()
                                }
                            } catch (e: Exception) {
                                if (isAdded) requireActivity().runOnUiThread {
                                    Toast.makeText(context, "Failed: ${e.message}", Toast.LENGTH_SHORT).show()
                                }
                            }
                        }.start()
                    }
                    .setNegativeButton(getString(R.string.cancel), null)
                    .show()
                true
            }
            terminalListContainer.addView(view)
        }
    }

    private fun buildSessionList(sessions: List<String>) {
        sessionListContainer.removeAllViews()
        if (sessions.isEmpty()) {
            tvSessionsHeader.visibility = View.GONE
            return
        }

        tvSessionsHeader.visibility = View.VISIBLE

        for (name in sessions) {
            val view = LayoutInflater.from(context)
                .inflate(R.layout.item_terminal, sessionListContainer, false)
            view.findViewById<TextView>(R.id.tvName).text = name
            view.findViewById<TextView>(R.id.tvDetail).text = getString(R.string.live_session)
            view.findViewById<View>(R.id.statusDot).setBackgroundResource(R.drawable.bg_status_dot_online)
            view.findViewById<ImageView>(R.id.ivPlatform).setImageResource(R.drawable.ic_computer)

            view.setOnClickListener {
                startActivity(Intent(requireContext(), LiveSessionActivity::class.java).apply {
                    putExtra(LiveSessionActivity.EXTRA_SESSION_NAME, name)
                })
            }

            sessionListContainer.addView(view)
        }
    }

    private fun buildFeed(feed: List<org.json.JSONObject>) {
        feedListContainer.removeAllViews()
        if (feed.isEmpty()) {
            cardFeed.visibility = View.GONE
            return
        }
        cardFeed.visibility = View.VISIBLE

        val sdf = java.text.SimpleDateFormat("HH:mm", java.util.Locale.getDefault())
        val now = System.currentTimeMillis()

        for ((index, entry) in feed.withIndex()) {
            val ts = entry.optLong("ts", 0) * 1000
            val tool = entry.optString("tool", "?")
            val ok = entry.optBoolean("ok", true)
            val terminal = entry.optString("terminal", "")
            val duration = entry.optDouble("duration", 0.0)

            val view = LayoutInflater.from(context).inflate(R.layout.item_feed, feedListContainer, false)

            // Tool name with icon
            val toolName = tool.removePrefix("remote_")
            val icon = when (toolName) {
                "run" -> "\u25B6"       // play
                "read" -> "\uD83D\uDCC4" // page
                "edit" -> "\u270F"       // pencil
                "glob" -> "\uD83D\uDD0D" // search
                "grep" -> "\uD83D\uDD0E" // search right
                "send" -> "\uD83D\uDCE4" // outbox
                "clip" -> "\uD83D\uDCCB" // clipboard
                "ls" -> "\uD83D\uDCCB"   // list
                "kill" -> "\u26D4"       // stop
                else -> "\u2022"         // bullet
            }
            view.findViewById<TextView>(R.id.tvFeedTool).text = "$icon $toolName"

            // Time: show relative for recent, absolute for older
            val ago = (now - ts) / 1000
            val timeStr = when {
                ago < 60 -> "${ago}s ago"
                ago < 3600 -> "${ago / 60}m ago"
                else -> sdf.format(java.util.Date(ts))
            }
            view.findViewById<TextView>(R.id.tvFeedTime).text = timeStr

            // Target terminal
            val tvTarget = view.findViewById<TextView>(R.id.tvFeedTarget)
            if (terminal.isNotEmpty()) {
                tvTarget.text = "\u2192 $terminal"
                tvTarget.visibility = View.VISIBLE
            } else {
                tvTarget.visibility = View.GONE
            }

            // Detail line: show command/path/pattern + duration
            val tvDetail = view.findViewById<TextView>(R.id.tvFeedDetail)
            val detail = when (toolName) {
                "run" -> entry.optString("command", "").take(60)
                "read", "edit" -> entry.optString("path", "").let {
                    if (it.length > 40) "...${it.takeLast(37)}" else it
                }
                "glob", "grep" -> entry.optString("pattern", "")
                "send" -> entry.optString("file", "").ifEmpty { entry.optString("source", "") }
                "clip" -> entry.optString("action", "")
                "ls" -> "${entry.optInt("count", 0)} terminals"
                else -> ""
            }
            val durationStr = if (duration > 0) {
                if (duration < 1) "${(duration * 1000).toInt()}ms" else "${duration}s"
            } else ""
            val fullDetail = listOfNotNull(
                detail.ifEmpty { null },
                durationStr.ifEmpty { null }
            ).joinToString("  \u00B7  ")

            if (fullDetail.isNotEmpty()) {
                tvDetail.text = fullDetail
                tvDetail.visibility = View.VISIBLE
            } else {
                tvDetail.visibility = View.GONE
            }

            // Status dot
            view.findViewById<View>(R.id.feedDot).setBackgroundResource(
                if (ok) R.drawable.bg_feed_ok else R.drawable.bg_feed_fail
            )

            // Timeline line
            if (index == feed.lastIndex) {
                view.findViewById<View>(R.id.timelineLine).visibility = View.INVISIBLE
            }

            feedListContainer.addView(view)
        }
    }

    // ---- GPU Metrics ----

    // Expanded state per terminal
    private val gpuExpanded = mutableSetOf<String>()

    private fun setupGpuIntervalButtons(view: View) {
        val btnOff = view.findViewById<com.google.android.material.button.MaterialButton>(R.id.btnGpuOff)
        val btn30s = view.findViewById<com.google.android.material.button.MaterialButton>(R.id.btnGpu30s)
        val btn1m = view.findViewById<com.google.android.material.button.MaterialButton>(R.id.btnGpu1m)
        val btn5m = view.findViewById<com.google.android.material.button.MaterialButton>(R.id.btnGpu5m)

        val buttons = listOf(btnOff to 0L, btn30s to 30_000L, btn1m to 60_000L, btn5m to 300_000L)

        fun highlight(active: com.google.android.material.button.MaterialButton) {
            for ((btn, _) in buttons) {
                if (btn == active) {
                    btn.backgroundTintList = ColorStateList.valueOf(
                        ContextCompat.getColor(requireContext(), R.color.primary))
                    btn.setTextColor(ContextCompat.getColor(requireContext(), R.color.on_primary))
                } else {
                    btn.backgroundTintList = ColorStateList.valueOf(0x00000000)
                    btn.setTextColor(ContextCompat.getColor(requireContext(), R.color.on_surface_variant))
                }
            }
        }

        highlight(btnOff)

        for ((btn, interval) in buttons) {
            btn.setOnClickListener {
                gpuRefreshInterval = interval
                highlight(btn)
                stopGpuPolling()
                if (interval > 0) startGpuPolling()
                fetchGpuMetrics()
            }
        }
    }

    private fun startGpuPolling() {
        stopGpuPolling()
        if (gpuRefreshInterval <= 0) return
        gpuRunnable = object : Runnable {
            override fun run() {
                fetchGpuMetrics()
                handler.postDelayed(this, gpuRefreshInterval)
            }
        }
        handler.postDelayed(gpuRunnable!!, gpuRefreshInterval)
    }

    private fun stopGpuPolling() {
        gpuRunnable?.let { handler.removeCallbacks(it) }
        gpuRunnable = null
    }

    private fun fetchGpuMetrics() {
        val r = relay ?: return
        val targets = gpuTerminals.toList()
        if (targets.isEmpty()) return

        // Single background thread: fetch all terminals sequentially,
        // update UI after each one arrives (no missing terminals)
        Thread {
            for (terminal in targets) {
                try {
                    val taskId = r.submitTask(terminal, "metrics", mapOf("tail" to 10))
                    val deadline = System.currentTimeMillis() + 45_000
                    var got = false
                    while (System.currentTimeMillis() < deadline) {
                        val task = r.pollTaskById(terminal, taskId)
                        if (task != null && task.optString("status") in listOf("DONE", "FAILED")) {
                            cachedGpuData[terminal] = task.optString("output", "")
                            r.clearTaskById(terminal, taskId)
                            got = true
                            break
                        }
                        Thread.sleep(2000)
                    }
                    if (!got) {
                        cachedGpuData[terminal] = "Timeout"
                    }
                } catch (e: Exception) {
                    cachedGpuData[terminal] = "Error: ${e.message}"
                }
                // Update UI after each terminal
                if (isAdded) {
                    requireActivity().runOnUiThread { buildGpuCards() }
                }
            }
        }.start()
    }

    private fun buildGpuCards() {
        gpuCardContainer.removeAllViews()

        if (cachedGpuData.isEmpty()) {
            tvGpuHeader.visibility = View.GONE
            gpuControlRow.visibility = View.GONE
            return
        }

        tvGpuHeader.visibility = View.VISIBLE
        gpuControlRow.visibility = View.VISIBLE

        for ((terminal, data) in cachedGpuData) {
            val strip = LayoutInflater.from(context)
                .inflate(R.layout.item_gpu_strip, gpuCardContainer, false)

            val isExpanded = terminal in gpuExpanded

            strip.findViewById<TextView>(R.id.tvStripName).text = terminal

            // Parse first GPU line for the compact strip summary
            val gpuInfo = parseFirstGpu(data)

            if (gpuInfo != null) {
                // Online / has data
                strip.findViewById<View>(R.id.gpuDot).setBackgroundResource(
                    if (gpuInfo.util > 10) R.drawable.bg_status_dot_online
                    else R.drawable.bg_feed_ok  // idle but reachable
                )
                strip.findViewById<ProgressBar>(R.id.pbStripUtil).progress = gpuInfo.util
                strip.findViewById<TextView>(R.id.tvStripUtil).text = "${gpuInfo.util}%"
                strip.findViewById<TextView>(R.id.tvStripUtil).setTextColor(percentColor(gpuInfo.util))
                strip.findViewById<TextView>(R.id.tvStripVram).text =
                    "${formatMiB(gpuInfo.vramUsed)}/${formatMiB(gpuInfo.vramTotal)}"
                strip.findViewById<TextView>(R.id.tvStripTemp).text = "${gpuInfo.temp}\u00B0"
            } else {
                // Error or no data
                strip.findViewById<View>(R.id.gpuDot).setBackgroundResource(R.drawable.bg_status_dot_offline)
                strip.findViewById<TextView>(R.id.tvStripUtil).text = "--"
                strip.findViewById<TextView>(R.id.tvStripVram).text = data.take(20)
                strip.findViewById<TextView>(R.id.tvStripTemp).text = ""
                strip.findViewById<ProgressBar>(R.id.pbStripUtil).progress = 0
            }

            // Expanded detail
            val expandedView = strip.findViewById<LinearLayout>(R.id.expandedDetail)
            expandedView.visibility = if (isExpanded) View.VISIBLE else View.GONE

            if (isExpanded && data.isNotEmpty()) {
                fillExpandedDetail(strip, data)
            }

            // Toggle expand on click
            strip.setOnClickListener {
                if (terminal in gpuExpanded) {
                    gpuExpanded.remove(terminal)
                } else {
                    gpuExpanded.add(terminal)
                }
                buildGpuCards()
            }

            gpuCardContainer.addView(strip)
        }
    }

    data class GpuSummary(val util: Int, val vramUsed: String, val vramTotal: String,
                          val vramPct: Int, val temp: Int, val power: String)

    private val gpuLineRegex = Regex(
        """GPU \d+: .+? \| (\d+)°C \| util (\d+)% \| VRAM ([\d.]+)/([\d.]+) MiB \((\d+)%\) \| ([\d.]+)W"""
    )

    private fun parseFirstGpu(data: String): GpuSummary? {
        // Find first GPU line, or aggregate across all GPUs
        val matches = gpuLineRegex.findAll(data).toList()
        if (matches.isEmpty()) return null

        // If multiple GPUs, show max util and sum VRAM
        var maxUtil = 0
        var totalVramUsed = 0f
        var totalVramTotal = 0f
        var maxTemp = 0
        var totalPower = 0f

        for (m in matches) {
            val util = m.groupValues[2].toIntOrNull() ?: 0
            val vUsed = m.groupValues[3].toFloatOrNull() ?: 0f
            val vTotal = m.groupValues[4].toFloatOrNull() ?: 0f
            val temp = m.groupValues[1].toIntOrNull() ?: 0
            val power = m.groupValues[6].toFloatOrNull() ?: 0f

            if (util > maxUtil) maxUtil = util
            totalVramUsed += vUsed
            totalVramTotal += vTotal
            if (temp > maxTemp) maxTemp = temp
            totalPower += power
        }

        val vramPct = if (totalVramTotal > 0) (totalVramUsed / totalVramTotal * 100).toInt() else 0

        return GpuSummary(
            util = maxUtil,
            vramUsed = totalVramUsed.toString(),
            vramTotal = totalVramTotal.toString(),
            vramPct = vramPct,
            temp = maxTemp,
            power = String.format("%.0f", totalPower)
        )
    }

    private fun fillExpandedDetail(strip: View, data: String) {
        val detailRows = strip.findViewById<LinearLayout>(R.id.detailGpuRows)
        val tvTraining = strip.findViewById<TextView>(R.id.tvDetailTraining)
        val tvProcs = strip.findViewById<TextView>(R.id.tvDetailProcs)

        detailRows.removeAllViews()

        val sections = data.split("\n\n")
        for (section in sections) {
            if (section.startsWith("## GPU Status")) {
                // Parse each GPU line into a detail row
                for (line in section.split("\n")) {
                    val m = Regex("""GPU (\d+): (.+?) \| (\d+)°C \| util (\d+)% \| VRAM ([\d.]+)/([\d.]+) MiB \((\d+)%\) \| ([\d.]+)W""")
                        .find(line) ?: continue
                    val (idx, name, temp, util, vUsed, vTotal, vPct, power) = m.destructured

                    val row = LayoutInflater.from(context)
                        .inflate(R.layout.item_gpu_detail_row, detailRows, false)

                    row.findViewById<TextView>(R.id.tvDetailGpuName).text = "GPU $idx: $name"

                    val utilVal = util.toIntOrNull() ?: 0
                    row.findViewById<TextView>(R.id.tvDtlUtil).text = "${util}%"
                    row.findViewById<TextView>(R.id.tvDtlUtil).setTextColor(percentColor(utilVal))
                    row.findViewById<ProgressBar>(R.id.pbDtlUtil).progress = utilVal

                    val vPctVal = vPct.toIntOrNull() ?: 0
                    row.findViewById<TextView>(R.id.tvDtlVram).text = "${vPct}%"
                    row.findViewById<TextView>(R.id.tvDtlVram).setTextColor(percentColor(vPctVal))
                    row.findViewById<TextView>(R.id.tvDtlVramSub).text =
                        "${formatMiB(vUsed)}/${formatMiB(vTotal)}"
                    row.findViewById<ProgressBar>(R.id.pbDtlVram).progress = vPctVal

                    val tempVal = temp.toIntOrNull() ?: 0
                    row.findViewById<TextView>(R.id.tvDtlTemp).text = "${temp}\u00B0"
                    row.findViewById<TextView>(R.id.tvDtlTemp).setTextColor(
                        percentColor(if (tempVal > 80) 95 else if (tempVal > 65) 75 else 40))

                    row.findViewById<TextView>(R.id.tvDtlPower).text = "${power}W"

                    detailRows.addView(row)
                }

                // Processes
                val procLines = section.split("\n").filter { it.trimStart().startsWith("GPU ") && it.contains("PID") }
                if (procLines.isNotEmpty()) {
                    tvProcs.text = procLines.joinToString("\n") { it.trim() }
                    tvProcs.visibility = View.VISIBLE
                }

            } else if (section.startsWith("## Training Metrics")) {
                // Show training summary
                val summaryLines = mutableListOf<String>()
                for (line in section.split("\n")) {
                    if (line.startsWith("Latest step:")) {
                        summaryLines.add(line)
                    }
                    if (line.contains(": ") && line.contains(" | ") && !line.startsWith("#")) {
                        summaryLines.add(line)
                    }
                }
                if (summaryLines.isNotEmpty()) {
                    tvTraining.text = summaryLines.joinToString("\n")
                    tvTraining.visibility = View.VISIBLE
                }
            }
        }
    }

    private fun percentColor(pct: Int): Int {
        return when {
            pct >= 90 -> 0xFFFF5577.toInt()
            pct >= 70 -> 0xFFFFBF24.toInt()
            else -> 0xFF00E5FF.toInt()
        }
    }

    private fun formatMiB(mib: String): String {
        val v = mib.toFloatOrNull() ?: return mib
        return if (v >= 1024) String.format("%.1fG", v / 1024) else "${v.toInt()}M"
    }
}
