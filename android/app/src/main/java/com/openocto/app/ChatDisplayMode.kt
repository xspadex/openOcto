package com.openocto.app

import androidx.recyclerview.widget.LinearLayoutManager
import androidx.recyclerview.widget.RecyclerView

class ChatDisplayMode(
    private val recyclerView: RecyclerView
) : DisplayMode {

    val adapter = ChatAdapter()

    init {
        recyclerView.layoutManager = LinearLayoutManager(recyclerView.context).apply {
            stackFromEnd = true
        }
        recyclerView.adapter = adapter
    }

    override fun addMessage(item: ChatItem.Message) {
        adapter.addItem(item)
        scrollToBottom()
    }

    override fun addToolCall(item: ChatItem.ToolCall) {
        adapter.addItem(item)
        scrollToBottom()
    }

    override fun addConfirmation(item: ChatItem.Confirmation) {
        adapter.addItem(item)
        scrollToBottom()
    }

    override fun addRoundSummary(item: ChatItem.RoundSummary) {
        adapter.addItem(item)
        scrollToBottom()
    }

    override fun updateToolCall(toolUseId: String, status: ChatItem.ToolStatus,
                                result: String?, durationMs: Long) {
        adapter.updateToolCall(toolUseId, status, result, durationMs)
    }

    override fun updateRoundSummary(roundId: String, toolCount: Int, totalMs: Long, isComplete: Boolean) {
        adapter.updateRoundSummary(roundId, toolCount, totalMs, isComplete)
    }

    override fun addStreamingMessage(item: ChatItem.StreamingMessage) {
        adapter.addItem(item)
        scrollToBottom()
    }

    private var scrollPending = false
    private val scrollHandler = android.os.Handler(android.os.Looper.getMainLooper())

    override fun appendStreamChunk(messageId: String, chunk: String) {
        adapter.appendStreamChunk(messageId, chunk)
        if (!scrollPending) {
            scrollPending = true
            scrollHandler.postDelayed({
                scrollToBottom()
                scrollPending = false
            }, 200)
        }
    }

    override fun completeStream(messageId: String) {
        adapter.completeStream(messageId)
    }

    override fun scrollToBottom() {
        recyclerView.post {
            val count = adapter.itemCount
            if (count > 0) recyclerView.smoothScrollToPosition(count - 1)
        }
    }

    override fun clear() {
        adapter.clear()
    }
}
