package com.openocto.app

import android.content.ClipData
import android.content.ClipboardManager
import android.content.Context
import android.view.Gravity
import android.view.LayoutInflater
import android.view.View
import android.view.ViewGroup
import android.widget.FrameLayout
import android.widget.TextView
import android.widget.Toast
import androidx.core.content.ContextCompat
import androidx.recyclerview.widget.RecyclerView
import io.noties.markwon.Markwon
import io.noties.markwon.ext.strikethrough.StrikethroughPlugin
import io.noties.markwon.ext.tables.TablePlugin

class ChatAdapter : RecyclerView.Adapter<RecyclerView.ViewHolder>() {

    companion object {
        const val TYPE_MESSAGE = 0
        const val TYPE_TOOL = 1
        const val TYPE_CONFIRM = 2
        const val TYPE_ROUND = 3
        const val TYPE_STREAMING = 4
    }

    private val items = mutableListOf<ChatItem>()

    /** Callback for confirmation responses. */
    var onConfirmResponse: ((confirmId: String, approved: Boolean) -> Unit)? = null

    fun addItem(item: ChatItem) {
        items.add(item)
        notifyItemInserted(items.size - 1)
    }

    fun updateToolCall(toolUseId: String, status: ChatItem.ToolStatus, result: String?, durationMs: Long) {
        val idx = items.indexOfFirst { it is ChatItem.ToolCall && it.toolUseId == toolUseId }
        if (idx >= 0) {
            val tc = items[idx] as ChatItem.ToolCall
            tc.status = status
            tc.result = result
            tc.durationMs = durationMs
            notifyItemChanged(idx)
        }
    }

    fun updateConfirmation(confirmId: String, approved: Boolean) {
        val idx = items.indexOfFirst { it is ChatItem.Confirmation && it.confirmId == confirmId }
        if (idx >= 0) {
            val c = items[idx] as ChatItem.Confirmation
            c.answered = true
            c.approved = approved
            notifyItemChanged(idx)
        }
    }

    fun updateRoundSummary(roundId: String, toolCount: Int, totalMs: Long, isComplete: Boolean) {
        val idx = items.indexOfFirst { it is ChatItem.RoundSummary && it.roundId == roundId }
        if (idx >= 0) {
            val r = items[idx] as ChatItem.RoundSummary
            r.toolCount = toolCount
            r.totalDurationMs = totalMs
            r.isComplete = isComplete
            notifyItemChanged(idx)
        }
    }

    private var streamDirty = false
    private var streamIdx = -1
    private val streamHandler = android.os.Handler(android.os.Looper.getMainLooper())
    private val streamFlush = Runnable {
        if (streamDirty && streamIdx >= 0 && streamIdx < items.size) {
            notifyItemChanged(streamIdx)
            streamDirty = false
        }
    }

    fun appendStreamChunk(messageId: String, chunk: String) {
        val idx = items.indexOfFirst { it is ChatItem.StreamingMessage && it.id == messageId }
        if (idx >= 0) {
            (items[idx] as ChatItem.StreamingMessage).text.append(chunk)
            streamIdx = idx
            if (!streamDirty) {
                streamDirty = true
                streamHandler.postDelayed(streamFlush, 80) // max 12.5 fps
            }
        }
    }

    fun completeStream(messageId: String) {
        streamHandler.removeCallbacks(streamFlush)
        val idx = items.indexOfFirst { it is ChatItem.StreamingMessage && it.id == messageId }
        if (idx >= 0) {
            (items[idx] as ChatItem.StreamingMessage).isComplete = true
            notifyItemChanged(idx)
        }
        streamDirty = false
        streamIdx = -1
    }

    fun getItems(): List<ChatItem> = items.toList()

    fun clear() {
        val size = items.size
        items.clear()
        notifyItemRangeRemoved(0, size)
    }

    override fun getItemCount() = items.size

    override fun getItemViewType(position: Int): Int {
        return when (items[position]) {
            is ChatItem.Message -> TYPE_MESSAGE
            is ChatItem.ToolCall -> TYPE_TOOL
            is ChatItem.Confirmation -> TYPE_CONFIRM
            is ChatItem.RoundSummary -> TYPE_ROUND
            is ChatItem.StreamingMessage -> TYPE_STREAMING
        }
    }

    override fun onCreateViewHolder(parent: ViewGroup, viewType: Int): RecyclerView.ViewHolder {
        val inflater = LayoutInflater.from(parent.context)
        return when (viewType) {
            TYPE_MESSAGE -> MessageVH(inflater.inflate(R.layout.item_chat_message, parent, false))
            TYPE_TOOL -> ToolVH(inflater.inflate(R.layout.item_tool_card, parent, false))
            TYPE_CONFIRM -> ConfirmVH(inflater.inflate(R.layout.item_confirm_card, parent, false))
            TYPE_ROUND -> RoundVH(inflater.inflate(R.layout.item_round_summary, parent, false))
            TYPE_STREAMING -> StreamVH(inflater.inflate(R.layout.item_chat_message, parent, false))
            else -> throw IllegalArgumentException()
        }
    }

    override fun onBindViewHolder(holder: RecyclerView.ViewHolder, position: Int) {
        when (holder) {
            is MessageVH -> holder.bind(items[position] as ChatItem.Message)
            is ToolVH -> holder.bind(items[position] as ChatItem.ToolCall)
            is ConfirmVH -> holder.bind(items[position] as ChatItem.Confirmation, this)
            is RoundVH -> holder.bind(items[position] as ChatItem.RoundSummary)
            is StreamVH -> holder.bind(items[position] as ChatItem.StreamingMessage)
        }
    }

    // ---- Message ViewHolder ----

    class MessageVH(view: View) : RecyclerView.ViewHolder(view) {
        private val tv: TextView = view.findViewById(R.id.tvMessage)
        private val markwon: Markwon by lazy {
            Markwon.builder(tv.context)
                .usePlugin(StrikethroughPlugin.create())
                .usePlugin(TablePlugin.create(tv.context))
                .build()
        }

        fun bind(item: ChatItem.Message) {
            val ctx = tv.context
            val lp = tv.layoutParams as FrameLayout.LayoutParams

            if (item.role == ChatItem.Role.USER) {
                tv.text = item.text
                lp.gravity = Gravity.END
                tv.setBackgroundResource(R.drawable.bg_bubble_user)
                tv.setTextColor(ContextCompat.getColor(ctx, R.color.bubble_user_text))
            } else {
                // Render Markdown for AI messages
                if (item.spannablePrefix != null) {
                    // Pixel art prefix + markdown text
                    tv.typeface = android.graphics.Typeface.MONOSPACE
                    val combined = android.text.SpannableStringBuilder()
                    combined.append(item.spannablePrefix)
                    combined.append(item.text)
                    tv.text = combined
                } else {
                    tv.typeface = android.graphics.Typeface.DEFAULT
                    markwon.setMarkdown(tv, item.text)
                }
                lp.gravity = Gravity.START
                tv.setBackgroundResource(R.drawable.bg_bubble_ai)
                tv.setTextColor(ContextCompat.getColor(ctx, R.color.bubble_ai_text))
            }
            tv.layoutParams = lp

            // Long press to copy
            tv.setOnLongClickListener {
                val clipboard = ctx.getSystemService(Context.CLIPBOARD_SERVICE) as ClipboardManager
                clipboard.setPrimaryClip(ClipData.newPlainText("octo", item.text))
                Toast.makeText(ctx, "Copied", Toast.LENGTH_SHORT).show()
                true
            }
        }
    }

    // ---- Tool ViewHolder ----

    class ToolVH(view: View) : RecyclerView.ViewHolder(view) {
        private val root: View = view
        private val timelineBar: View = view.findViewById(R.id.timelineBar)
        private val tvIcon: TextView = view.findViewById(R.id.tvToolIcon)
        private val tvName: TextView = view.findViewById(R.id.tvToolName)
        private val tvTarget: TextView = view.findViewById(R.id.tvToolTarget)
        private val tvStatus: TextView = view.findViewById(R.id.tvToolStatus)
        private val tvArgs: TextView = view.findViewById(R.id.tvToolArgs)
        private val tvPreview: TextView = view.findViewById(R.id.tvResultPreview)
        private val scrollResult: View = view.findViewById(R.id.scrollResult)
        private val tvResult: TextView = view.findViewById(R.id.tvToolResult)
        private val tvDuration: TextView = view.findViewById(R.id.tvToolDuration)

        fun bind(item: ChatItem.ToolCall) {
            val ctx = root.context

            tvIcon.text = toolIcon(item.toolName)
            tvName.text = item.toolName

            if (item.target != null) {
                tvTarget.text = item.target
                tvTarget.visibility = View.VISIBLE
            } else {
                tvTarget.visibility = View.GONE
            }

            tvArgs.text = toolArgSummary(item)
            tvArgs.visibility = if (tvArgs.text.isNotEmpty()) View.VISIBLE else View.GONE

            // Timeline bar color
            val barColor = when (item.status) {
                ChatItem.ToolStatus.RUNNING -> R.color.primary
                ChatItem.ToolStatus.DONE -> R.color.status_online
                ChatItem.ToolStatus.FAILED -> R.color.status_error
                else -> R.color.outline
            }
            timelineBar.setBackgroundColor(ContextCompat.getColor(ctx, barColor))

            when (item.status) {
                ChatItem.ToolStatus.PENDING -> {
                    tvStatus.text = "..."
                    tvStatus.setTextColor(ContextCompat.getColor(ctx, R.color.status_offline))
                }
                ChatItem.ToolStatus.RUNNING -> {
                    tvStatus.text = "\u25B6"
                    tvStatus.setTextColor(ContextCompat.getColor(ctx, R.color.primary))
                }
                ChatItem.ToolStatus.DONE -> {
                    tvStatus.text = "\u2713"
                    tvStatus.setTextColor(ContextCompat.getColor(ctx, R.color.status_online))
                }
                ChatItem.ToolStatus.FAILED -> {
                    tvStatus.text = "\u2717"
                    tvStatus.setTextColor(ContextCompat.getColor(ctx, R.color.status_error))
                }
            }

            if (item.durationMs > 0) {
                tvDuration.text = formatDuration(item.durationMs)
                tvDuration.visibility = View.VISIBLE
            } else {
                tvDuration.visibility = View.GONE
            }

            // Result preview (first 2 lines, always visible when done)
            if (item.result != null && !item.isExpanded) {
                val preview = item.result!!.lines().take(2).joinToString("\n").take(120)
                if (preview.isNotBlank()) {
                    tvPreview.text = "\u25B8 $preview"
                    tvPreview.visibility = View.VISIBLE
                } else {
                    tvPreview.visibility = View.GONE
                }
            } else {
                tvPreview.visibility = View.GONE
            }

            // Full result (expanded)
            if (item.result != null && item.isExpanded) {
                tvResult.text = item.result
                scrollResult.visibility = View.VISIBLE
            } else {
                scrollResult.visibility = View.GONE
            }

            root.setOnClickListener {
                if (item.result != null) {
                    item.isExpanded = !item.isExpanded
                    val pos = adapterPosition
                    if (pos != RecyclerView.NO_POSITION) {
                        (itemView.parent as? RecyclerView)?.adapter?.notifyItemChanged(pos)
                    }
                }
            }

            // Long press to copy result
            root.setOnLongClickListener {
                val text = item.result ?: return@setOnLongClickListener false
                val clipboard = ctx.getSystemService(Context.CLIPBOARD_SERVICE) as ClipboardManager
                clipboard.setPrimaryClip(ClipData.newPlainText("octo", text))
                Toast.makeText(ctx, "Result copied", Toast.LENGTH_SHORT).show()
                true
            }
        }
    }

    // ---- Confirmation ViewHolder ----

    class ConfirmVH(view: View) : RecyclerView.ViewHolder(view) {
        private val tvTitle: TextView = view.findViewById(R.id.tvConfirmTitle)
        private val tvDesc: TextView = view.findViewById(R.id.tvConfirmDesc)
        private val layoutButtons: View = view.findViewById(R.id.layoutButtons)
        private val btnAllow: View = view.findViewById(R.id.btnAllow)
        private val btnDeny: View = view.findViewById(R.id.btnDeny)
        private val tvResult: TextView = view.findViewById(R.id.tvConfirmResult)

        fun bind(item: ChatItem.Confirmation, adapter: ChatAdapter) {
            val ctx = tvTitle.context
            tvTitle.text = toolIcon(item.toolName) + " " + item.toolName
            tvDesc.text = item.description

            if (item.answered) {
                layoutButtons.visibility = View.GONE
                tvResult.visibility = View.VISIBLE
                if (item.approved) {
                    tvResult.text = "\u2713 Approved"
                    tvResult.setTextColor(ContextCompat.getColor(ctx, R.color.status_online))
                } else {
                    tvResult.text = "\u2717 Denied"
                    tvResult.setTextColor(ContextCompat.getColor(ctx, R.color.status_error))
                }
            } else {
                layoutButtons.visibility = View.VISIBLE
                tvResult.visibility = View.GONE
                btnAllow.setOnClickListener {
                    adapter.onConfirmResponse?.invoke(item.confirmId, true)
                    adapter.updateConfirmation(item.confirmId, true)
                }
                btnDeny.setOnClickListener {
                    adapter.onConfirmResponse?.invoke(item.confirmId, false)
                    adapter.updateConfirmation(item.confirmId, false)
                }
            }
        }
    }

    // ---- Round Summary ViewHolder ----

    class RoundVH(view: View) : RecyclerView.ViewHolder(view) {
        private val tvSummary: TextView = view.findViewById(R.id.tvRoundSummary)

        fun bind(item: ChatItem.RoundSummary) {
            if (item.isComplete) {
                tvSummary.text = "${item.toolCount} tool(s) \u2022 ${formatDuration(item.totalDurationMs)}"
            } else {
                tvSummary.text = "${item.toolCount} tool(s) running..."
            }
        }
    }

    // ---- Streaming Message ViewHolder ----

    class StreamVH(view: View) : RecyclerView.ViewHolder(view) {
        private val tv: TextView = view.findViewById(R.id.tvMessage)
        private val markwon: Markwon by lazy {
            Markwon.builder(tv.context)
                .usePlugin(StrikethroughPlugin.create())
                .usePlugin(TablePlugin.create(tv.context))
                .build()
        }

        fun bind(item: ChatItem.StreamingMessage) {
            val ctx = tv.context
            val text = item.text.toString()
            val lp = tv.layoutParams as FrameLayout.LayoutParams
            lp.gravity = Gravity.START
            tv.setBackgroundResource(R.drawable.bg_bubble_ai)
            tv.setTextColor(ContextCompat.getColor(ctx, R.color.bubble_ai_text))
            tv.layoutParams = lp

            if (text.isEmpty() && !item.isComplete) {
                tv.text = "..."
            } else if (item.isComplete) {
                // Final render with Markdown
                markwon.setMarkdown(tv, text)
            } else {
                // Streaming: plain text (Markdown mid-stream would be janky)
                tv.text = text
            }

            // Long press to copy
            tv.setOnLongClickListener {
                val clipboard = ctx.getSystemService(Context.CLIPBOARD_SERVICE) as ClipboardManager
                clipboard.setPrimaryClip(ClipData.newPlainText("octo", text))
                Toast.makeText(ctx, "Copied", Toast.LENGTH_SHORT).show()
                true
            }
        }
    }
}

// ---- Helpers (top-level for access from ViewHolders) ----

fun toolIcon(name: String): String = when {
    name.contains("sms") -> "\uD83D\uDCE7"
    name.contains("call") -> "\uD83D\uDCDE"
    name.contains("alarm") -> "\u23F0"
    name.contains("location") -> "\uD83D\uDCCD"
    name.contains("contact") -> "\uD83D\uDC64"
    name.contains("device_info") -> "\uD83D\uDCF1"
    name.contains("notification") -> "\uD83D\uDD14"
    name.contains("tts") -> "\uD83D\uDD0A"
    name.contains("vibrate") -> "\uD83D\uDCF3"
    name.contains("volume") -> "\uD83D\uDD09"
    name.contains("open_app") -> "\uD83D\uDCE6"
    name.contains("memory") || name.contains("recall") -> "\uD83E\uDDE0"
    name.contains("file") || name.contains("edit") || name.contains("cat") -> "\uD83D\uDCC4"
    name.contains("search") || name.contains("grep") || name.contains("glob") -> "\uD83D\uDD0D"
    name.contains("clipboard") -> "\uD83D\uDCCB"
    name.contains("wake") -> "\uD83D\uDD14"
    name == "list_terminals" -> "\uD83D\uDCE1"
    else -> "\u26A1"
}

fun toolArgSummary(item: ChatItem.ToolCall): String = when (item.toolName) {
    "run_command" -> "$ ${item.args["command"] ?: ""}"
    "read_file" -> item.args["path"] ?: ""
    "edit_file" -> item.args["path"] ?: ""
    "search_files" -> item.args["pattern"] ?: ""
    "search_content" -> item.args["pattern"] ?: ""
    "clipboard_write" -> item.args["text"]?.take(40) ?: ""
    "send_sms" -> "To: ${item.args["to"] ?: ""} | ${item.args["message"]?.take(30) ?: ""}"
    "make_call" -> item.args["number"] ?: ""
    "set_alarm" -> "${item.args["hour"] ?: "?"}:${item.args["minute"] ?: "?"} ${item.args["message"] ?: ""}"
    "read_contacts" -> item.args["search"] ?: "all"
    "tts" -> item.args["text"]?.take(40) ?: ""
    "open_app" -> item.args["package"] ?: ""
    "save_memory" -> item.args["content"]?.take(40) ?: ""
    else -> item.args.entries.take(3).joinToString(", ") { "${it.key}=${it.value}" }
}

fun formatDuration(ms: Long): String = when {
    ms < 1000 -> "${ms}ms"
    ms < 60000 -> String.format("%.1fs", ms / 1000.0)
    else -> "${ms / 60000}m ${(ms % 60000) / 1000}s"
}
