package com.openocto.app

/**
 * Data model for agent chat items.
 * Supports: text messages, tool call cards, confirmation requests, tool groups.
 */
sealed class ChatItem(val id: String, val timestamp: Long = System.currentTimeMillis()) {

    data class Message(
        val role: Role,
        val text: String,
        val spannablePrefix: android.text.SpannableStringBuilder? = null,
        val _id: String = "${System.currentTimeMillis()}-${(Math.random() * 10000).toInt()}"
    ) : ChatItem(_id)

    data class ToolCall(
        val toolUseId: String,
        val toolName: String,
        val target: String?,
        val args: Map<String, String>,
        var status: ToolStatus = ToolStatus.PENDING,
        var result: String? = null,
        var durationMs: Long = 0,
        var isExpanded: Boolean = false
    ) : ChatItem(toolUseId)

    /** Confirmation card for sensitive operations. */
    data class Confirmation(
        val confirmId: String,
        val toolName: String,
        val description: String,
        val args: Map<String, String>,
        var answered: Boolean = false,
        var approved: Boolean = false
    ) : ChatItem(confirmId)

    /** Group header showing total duration after a round of tool calls. */
    data class RoundSummary(
        val roundId: String,
        var toolCount: Int = 0,
        var totalDurationMs: Long = 0,
        var isComplete: Boolean = false
    ) : ChatItem(roundId)

    /** Streaming message — text is appended chunk by chunk. */
    data class StreamingMessage(
        val messageId: String,
        var text: StringBuilder = StringBuilder(),
        var isComplete: Boolean = false
    ) : ChatItem(messageId)

    enum class Role { USER, ASSISTANT }
    enum class ToolStatus { PENDING, RUNNING, DONE, FAILED }
}
