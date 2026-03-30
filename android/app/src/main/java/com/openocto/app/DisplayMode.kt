package com.openocto.app

interface DisplayMode {
    fun addMessage(item: ChatItem.Message)
    fun addToolCall(item: ChatItem.ToolCall)
    fun addConfirmation(item: ChatItem.Confirmation)
    fun addRoundSummary(item: ChatItem.RoundSummary)
    fun updateToolCall(toolUseId: String, status: ChatItem.ToolStatus, result: String?, durationMs: Long = 0)
    fun updateRoundSummary(roundId: String, toolCount: Int, totalMs: Long, isComplete: Boolean)
    fun addStreamingMessage(item: ChatItem.StreamingMessage)
    fun appendStreamChunk(messageId: String, chunk: String)
    fun completeStream(messageId: String)
    fun scrollToBottom()
    fun clear()
}
