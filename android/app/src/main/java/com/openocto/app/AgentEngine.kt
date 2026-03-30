package com.openocto.app

import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import org.json.JSONArray
import org.json.JSONObject
import java.util.concurrent.TimeUnit

/**
 * On-device agent engine with event-based callbacks.
 * Calls LLM API directly from the phone, executes octo tools via RelayClient.
 */
class AgentEngine(
    private val config: OctoConfig,
    private val relay: RelayClient,
    private val localTaskHandler: TaskHandler? = null,
    private val appContext: android.content.Context? = null,
    var currentPet: Pet? = null
) {
    sealed class Event {
        data class AssistantText(val text: String) : Event()
        /** Streaming: partial text chunk (append to current message). */
        data class TextChunk(val messageId: String, val chunk: String) : Event()
        /** Streaming: current message is complete. */
        data class TextDone(val messageId: String) : Event()
        data class ToolStart(val toolUseId: String, val toolName: String,
                             val target: String?, val args: Map<String, String>) : Event()
        data class ToolResult(val toolUseId: String, val result: String,
                              val success: Boolean, val durationMs: Long) : Event()
        data class ConfirmRequired(val confirmId: String, val toolName: String,
                                   val description: String, val args: Map<String, String>) : Event()
        data class RoundStart(val roundId: String) : Event()
        data class RoundEnd(val roundId: String, val toolCount: Int, val totalMs: Long) : Event()
        data class Error(val message: String) : Event()
        object Done : Event()
    }

    /** Tools that require user confirmation before execution. */
    private val sensitiveTools = setOf("send_sms", "make_call", "set_alarm", "open_app", "nearby_send", "nearby_receive")

    /** Confirmation callback — set by AgentActivity on UI thread. */
    @Volatile var confirmCallback: ((confirmId: String, approved: Boolean) -> Unit)? = null
    private val confirmResults = java.util.concurrent.ConcurrentHashMap<String, Boolean?>()

    /** Nearby P2P transfer manager (lazy — only created when needed). */
    private var nearbyTransfer: NearbyTransferManager? = null

    /** Memory directory on this phone. */
    private val memoryDir = java.io.File("/sdcard/.octo/memories").apply { mkdirs() }
    private val agentMdFile = java.io.File("/sdcard/.octo/octo-agent.md")

    private val client = OkHttpClient.Builder()
        .connectTimeout(30, TimeUnit.SECONDS)
        .readTimeout(120, TimeUnit.SECONDS)
        .build()

    private val history = mutableListOf<JSONObject>()
    private val JSON_MEDIA = "application/json; charset=utf-8".toMediaType()
    private val MAX_TOOL_ROUNDS = 8
    private val TASK_TIMEOUT = 90

    fun clearHistory() { history.clear() }

    /**
     * Process user message with event callbacks. Runs on calling thread (must be background).
     */
    fun chat(userMessage: String, onEvent: (Event) -> Unit) {
        val terminals = try { relay.listTerminals() } catch (_: Exception) { emptyList() }
        val systemPrompt = buildSystemPrompt(terminals)

        history.add(JSONObject().put("role", "user").put("content", userMessage))

        var rounds = MAX_TOOL_ROUNDS
        while (rounds > 0) {
            rounds--

            val llmResp = try {
                callLlmStreaming(systemPrompt, onEvent)
            } catch (e: Exception) {
                onEvent(Event.Error("API error: ${e.message}"))
                return
            }

            // Add assistant message to history
            history.add(llmResp.rawForHistory)

            // No tool calls → done
            if (llmResp.toolCalls.isEmpty()) {
                // If no streaming text was emitted (e.g. fallback), emit full text
                if (llmResp.text.isNotEmpty()) {
                    // TextChunk already emitted during streaming, no need to re-emit
                }
                onEvent(Event.Done)
                return
            }

            // Execute tool calls
            val roundId = "round-${System.currentTimeMillis()}"
            onEvent(Event.RoundStart(roundId))
            val roundStart = System.currentTimeMillis()
            var roundToolCount = 0

            if (isClaude()) {
                // Claude format: tool results go as user message with tool_result blocks
                val results = JSONArray()
                for (tc in llmResp.toolCalls) {
                    val args = jsonToMap(tc.arguments)
                    val target = args["target"]

                    onEvent(Event.ToolStart(tc.id, tc.name, target, args))
                    val startTime = System.currentTimeMillis()
                    val result = executeTool(tc.name, tc.arguments, onEvent)
                    val duration = System.currentTimeMillis() - startTime
                    val success = !result.startsWith("Error:") && !result.startsWith("User denied")
                    onEvent(Event.ToolResult(tc.id, result, success, duration))
                    roundToolCount++

                    results.put(JSONObject().apply {
                        put("type", "tool_result")
                        put("tool_use_id", tc.id)
                        put("content", result)
                    })
                }
                history.add(JSONObject().put("role", "user").put("content", results))
            } else {
                // OpenAI format: each tool result is a separate message
                for (tc in llmResp.toolCalls) {
                    val args = jsonToMap(tc.arguments)
                    val target = args["target"]

                    onEvent(Event.ToolStart(tc.id, tc.name, target, args))
                    val startTime = System.currentTimeMillis()
                    val result = executeTool(tc.name, tc.arguments, onEvent)
                    val duration = System.currentTimeMillis() - startTime
                    val success = !result.startsWith("Error:") && !result.startsWith("User denied")
                    onEvent(Event.ToolResult(tc.id, result, success, duration))
                    roundToolCount++

                    history.add(JSONObject().apply {
                        put("role", "tool")
                        put("tool_call_id", tc.id)
                        put("content", result)
                    })
                }
            }

            onEvent(Event.RoundEnd(roundId, roundToolCount, System.currentTimeMillis() - roundStart))
        }
        onEvent(Event.AssistantText("[Exceeded max tool rounds]"))
        onEvent(Event.Done)
    }

    // ---- LLM ----

    /** Parsed LLM response — text + optional tool calls. */
    data class LlmResponse(
        val text: String,
        val toolCalls: List<LlmToolCall>,
        val rawForHistory: JSONObject // original message to add to history
    )
    data class LlmToolCall(val id: String, val name: String, val arguments: JSONObject)

    private fun isClaude(): Boolean = config.aiProvider == "claude"

    private fun callLlm(systemPrompt: String): JSONObject {
        return if (isClaude()) callClaude(systemPrompt) else callOpenAi(systemPrompt)
    }

    /** Streaming LLM call — emits TextChunk events, returns parsed response. */
    private fun callLlmStreaming(systemPrompt: String, onEvent: (Event) -> Unit): LlmResponse {
        return if (isClaude()) callClaudeStreaming(systemPrompt, onEvent)
               else callOpenAiStreaming(systemPrompt, onEvent)
    }

    private fun callClaude(systemPrompt: String): JSONObject {
        val baseUrl = config.aiBaseUrl.ifEmpty { "https://api.anthropic.com" }.trimEnd('/')
        val model = config.aiModel.ifEmpty { "claude-sonnet-4-20250514" }

        val body = JSONObject().apply {
            put("model", model)
            put("max_tokens", 4096)
            put("system", systemPrompt)
            put("tools", claudeTools())
            put("messages", JSONArray(history.map { JSONObject(it.toString()) }))
        }

        val request = Request.Builder()
            .url("$baseUrl/v1/messages")
            .addHeader("x-api-key", config.aiApiKey)
            .addHeader("anthropic-version", "2023-06-01")
            .addHeader("content-type", "application/json")
            .post(body.toString().toRequestBody(JSON_MEDIA))
            .build()

        client.newCall(request).execute().use { resp ->
            val respBody = resp.body?.string() ?: throw Exception("Empty response")
            if (!resp.isSuccessful) throw Exception("${resp.code}: $respBody")
            return JSONObject(respBody)
        }
    }

    private fun callOpenAi(systemPrompt: String): JSONObject {
        val baseUrl = when (config.aiProvider) {
            "gemini" -> config.aiBaseUrl.ifEmpty { "https://generativelanguage.googleapis.com/v1beta/openai" }
            "ollama" -> config.aiBaseUrl.ifEmpty { "http://localhost:11434/v1" }
            "openrouter" -> config.aiBaseUrl.ifEmpty { "https://openrouter.ai/api/v1" }
            "siliconflow" -> config.aiBaseUrl.ifEmpty { "https://api.siliconflow.cn/v1" }
            "qwen" -> config.aiBaseUrl.ifEmpty { "https://dashscope.aliyuncs.com/compatible-mode/v1" }
            "kimi" -> config.aiBaseUrl.ifEmpty { "https://api.moonshot.cn/v1" }
            "minimax" -> config.aiBaseUrl.ifEmpty { "https://api.minimax.chat/v1" }
            else -> config.aiBaseUrl.ifEmpty { "https://api.openai.com/v1" }
        }.trimEnd('/')

        val model = config.aiModel.ifEmpty {
            when (config.aiProvider) {
                "gemini" -> "gemini-2.0-flash"
                "ollama" -> "llama3"
                "openrouter" -> "openai/gpt-4o-mini"
                "siliconflow" -> "Qwen/Qwen2.5-32B-Instruct"
                "qwen" -> "qwen-plus"
                "kimi" -> "moonshot-v1-8k"
                "minimax" -> "MiniMax-Text-01"
                else -> "gpt-4o-mini"
            }
        }

        val messages = JSONArray().apply {
            put(JSONObject().put("role", "system").put("content", systemPrompt))
            for (msg in history) put(msg)
        }

        val body = JSONObject().apply {
            put("model", model)
            put("messages", messages)
            put("tools", openAiTools())
        }

        val request = Request.Builder()
            .url("$baseUrl/chat/completions")
            .addHeader("Authorization", "Bearer ${config.aiApiKey}")
            .addHeader("content-type", "application/json")
            .post(body.toString().toRequestBody(JSON_MEDIA))
            .build()

        client.newCall(request).execute().use { resp ->
            val respBody = resp.body?.string() ?: throw Exception("Empty response")
            if (!resp.isSuccessful) throw Exception("${resp.code}: $respBody")
            return JSONObject(respBody)
        }
    }

    // ---- Streaming LLM Calls ----

    private fun callOpenAiStreaming(systemPrompt: String, onEvent: (Event) -> Unit): LlmResponse {
        val baseUrl = when (config.aiProvider) {
            "gemini" -> config.aiBaseUrl.ifEmpty { "https://generativelanguage.googleapis.com/v1beta/openai" }
            "ollama" -> config.aiBaseUrl.ifEmpty { "http://localhost:11434/v1" }
            "openrouter" -> config.aiBaseUrl.ifEmpty { "https://openrouter.ai/api/v1" }
            "siliconflow" -> config.aiBaseUrl.ifEmpty { "https://api.siliconflow.cn/v1" }
            "qwen" -> config.aiBaseUrl.ifEmpty { "https://dashscope.aliyuncs.com/compatible-mode/v1" }
            "kimi" -> config.aiBaseUrl.ifEmpty { "https://api.moonshot.cn/v1" }
            "minimax" -> config.aiBaseUrl.ifEmpty { "https://api.minimax.chat/v1" }
            else -> config.aiBaseUrl.ifEmpty { "https://api.openai.com/v1" }
        }.trimEnd('/')

        val model = config.aiModel.ifEmpty {
            when (config.aiProvider) {
                "gemini" -> "gemini-2.0-flash"; "ollama" -> "llama3"
                "openrouter" -> "openai/gpt-4o-mini"; "siliconflow" -> "Qwen/Qwen2.5-32B-Instruct"
                "qwen" -> "qwen-plus"; "kimi" -> "moonshot-v1-8k"; "minimax" -> "MiniMax-Text-01"
                else -> "gpt-4o-mini"
            }
        }

        val messages = JSONArray().apply {
            put(JSONObject().put("role", "system").put("content", systemPrompt))
            for (msg in history) put(msg)
        }

        val body = JSONObject().apply {
            put("model", model)
            put("messages", messages)
            put("tools", openAiTools())
            put("stream", true)
        }

        val request = Request.Builder()
            .url("$baseUrl/chat/completions")
            .addHeader("Authorization", "Bearer ${config.aiApiKey}")
            .addHeader("content-type", "application/json")
            .post(body.toString().toRequestBody(JSON_MEDIA))
            .build()

        val msgId = "msg-${System.currentTimeMillis()}"
        val textBuilder = StringBuilder()
        val reasoningBuilder = StringBuilder() // for thinking models (kimi k2.5, deepseek-r1, etc.)
        // Tool calls accumulator: id -> {name, arguments_chunks}
        val toolCallMap = mutableMapOf<Int, Triple<String, String, StringBuilder>>() // index -> (id, name, args)

        client.newCall(request).execute().use { resp ->
            if (!resp.isSuccessful) {
                val errBody = resp.body?.string() ?: ""
                throw Exception("${resp.code}: $errBody")
            }

            val reader = resp.body?.source() ?: throw Exception("Empty response")
            while (!reader.exhausted()) {
                val line = reader.readUtf8Line() ?: break
                if (!line.startsWith("data: ")) continue
                val data = line.removePrefix("data: ").trim()
                if (data == "[DONE]") break

                try {
                    val chunk = JSONObject(data)
                    val delta = chunk.optJSONArray("choices")?.optJSONObject(0)
                        ?.optJSONObject("delta") ?: continue

                    // Reasoning content (thinking models: kimi k2.5, deepseek-r1)
                    val reasoning = if (delta.isNull("reasoning_content")) "" else delta.optString("reasoning_content", "")
                    if (reasoning.isNotEmpty() && reasoning != "null") {
                        reasoningBuilder.append(reasoning)
                    }

                    // Text content (guard against Android optString returning "null" for JSON null)
                    val content = if (delta.isNull("content")) "" else delta.optString("content", "")
                    if (content.isNotEmpty() && content != "null") {
                        textBuilder.append(content)
                        onEvent(Event.TextChunk(msgId, content))
                    }

                    // Tool calls (streamed incrementally)
                    val tcArray = delta.optJSONArray("tool_calls")
                    if (tcArray != null) {
                        for (i in 0 until tcArray.length()) {
                            val tc = tcArray.getJSONObject(i)
                            val idx = tc.optInt("index", i)
                            val fn = tc.optJSONObject("function")

                            if (!toolCallMap.containsKey(idx)) {
                                val id = tc.optString("id", "call_$idx")
                                val name = fn?.optString("name", "") ?: ""
                                toolCallMap[idx] = Triple(id, name, StringBuilder())
                            }

                            val argsChunk = if (fn?.isNull("arguments") != false) "" else fn.optString("arguments", "")
                            if (argsChunk.isNotEmpty() && argsChunk != "null") {
                                toolCallMap[idx]!!.third.append(argsChunk)
                            }
                        }
                    }
                } catch (_: Exception) {}
            }
        }

        if (textBuilder.isNotEmpty()) {
            onEvent(Event.TextDone(msgId))
        }

        // Build tool calls
        val toolCalls = toolCallMap.values.map { (id, name, argsBuilder) ->
            val args = try { JSONObject(argsBuilder.toString()) } catch (_: Exception) { JSONObject() }
            LlmToolCall(id, name, args)
        }

        // Build raw message for history
        val rawMsg = JSONObject().apply {
            put("role", "assistant")
            put("content", textBuilder.toString())
            // Preserve reasoning_content for thinking models (kimi k2.5, deepseek-r1)
            if (reasoningBuilder.isNotEmpty()) {
                put("reasoning_content", reasoningBuilder.toString())
            }
            if (toolCalls.isNotEmpty()) {
                val tcArr = JSONArray()
                for (tc in toolCalls) {
                    tcArr.put(JSONObject().apply {
                        put("id", tc.id)
                        put("type", "function")
                        put("function", JSONObject().apply {
                            put("name", tc.name)
                            put("arguments", tc.arguments.toString())
                        })
                    })
                }
                put("tool_calls", tcArr)
            }
        }

        return LlmResponse(textBuilder.toString(), toolCalls, rawMsg)
    }

    private fun callClaudeStreaming(systemPrompt: String, onEvent: (Event) -> Unit): LlmResponse {
        val baseUrl = config.aiBaseUrl.ifEmpty { "https://api.anthropic.com" }.trimEnd('/')
        val model = config.aiModel.ifEmpty { "claude-sonnet-4-20250514" }

        val body = JSONObject().apply {
            put("model", model)
            put("max_tokens", 4096)
            put("system", systemPrompt)
            put("tools", claudeTools())
            put("messages", JSONArray(history.map { JSONObject(it.toString()) }))
            put("stream", true)
        }

        val request = Request.Builder()
            .url("$baseUrl/v1/messages")
            .addHeader("x-api-key", config.aiApiKey)
            .addHeader("anthropic-version", "2023-06-01")
            .addHeader("content-type", "application/json")
            .post(body.toString().toRequestBody(JSON_MEDIA))
            .build()

        val msgId = "msg-${System.currentTimeMillis()}"
        val textBuilder = StringBuilder()
        val toolUses = mutableListOf<LlmToolCall>()
        var currentToolId = ""
        var currentToolName = ""
        val currentToolArgs = StringBuilder()

        client.newCall(request).execute().use { resp ->
            if (!resp.isSuccessful) {
                val errBody = resp.body?.string() ?: ""
                throw Exception("${resp.code}: $errBody")
            }

            val reader = resp.body?.source() ?: throw Exception("Empty response")
            while (!reader.exhausted()) {
                val line = reader.readUtf8Line() ?: break
                if (!line.startsWith("data: ")) continue
                val data = line.removePrefix("data: ").trim()
                if (data.isEmpty()) continue

                try {
                    val event = JSONObject(data)
                    when (event.optString("type")) {
                        "content_block_start" -> {
                            val block = event.optJSONObject("content_block")
                            if (block?.optString("type") == "tool_use") {
                                currentToolId = block.optString("id", "")
                                currentToolName = block.optString("name", "")
                                currentToolArgs.clear()
                            }
                        }
                        "content_block_delta" -> {
                            val delta = event.optJSONObject("delta")
                            when (delta?.optString("type")) {
                                "text_delta" -> {
                                    val text = if (delta.isNull("text")) "" else delta.optString("text", "")
                                    if (text.isNotEmpty() && text != "null") {
                                        textBuilder.append(text)
                                        onEvent(Event.TextChunk(msgId, text))
                                    }
                                }
                                "input_json_delta" -> {
                                    val pj = if (delta.isNull("partial_json")) "" else delta.optString("partial_json", "")
                                    if (pj.isNotEmpty() && pj != "null") currentToolArgs.append(pj)
                                }
                            }
                        }
                        "content_block_stop" -> {
                            if (currentToolId.isNotEmpty()) {
                                val args = try { JSONObject(currentToolArgs.toString()) }
                                           catch (_: Exception) { JSONObject() }
                                toolUses.add(LlmToolCall(currentToolId, currentToolName, args))
                                currentToolId = ""
                                currentToolName = ""
                                currentToolArgs.clear()
                            }
                        }
                    }
                } catch (_: Exception) {}
            }
        }

        if (textBuilder.isNotEmpty()) {
            onEvent(Event.TextDone(msgId))
        }

        // Build raw content for history
        val contentArr = JSONArray()
        if (textBuilder.isNotEmpty()) {
            contentArr.put(JSONObject().put("type", "text").put("text", textBuilder.toString()))
        }
        for (tu in toolUses) {
            contentArr.put(JSONObject().apply {
                put("type", "tool_use")
                put("id", tu.id)
                put("name", tu.name)
                put("input", tu.arguments)
            })
        }
        val rawMsg = JSONObject().put("role", "assistant").put("content", contentArr)

        return LlmResponse(textBuilder.toString(), toolUses, rawMsg)
    }

    // ---- Tools ----

    private data class ToolDef(val name: String, val description: String,
                               val properties: Map<String, Map<String, String>>,
                               val required: List<String> = emptyList())

    private val toolDefs = listOf(
        ToolDef("list_terminals", "List all registered terminals with status, platform, and tags",
            emptyMap()),
        ToolDef("run_command", "Execute a shell command on a remote terminal",
            mapOf("target" to mapOf("type" to "string", "description" to "Terminal name"),
                  "command" to mapOf("type" to "string", "description" to "Shell command")),
            listOf("target", "command")),
        ToolDef("read_file", "Read a file on a remote terminal with line numbers",
            mapOf("target" to mapOf("type" to "string", "description" to "Terminal name"),
                  "path" to mapOf("type" to "string", "description" to "File path")),
            listOf("target", "path")),
        ToolDef("edit_file", "Edit a file on a remote terminal by replacing text",
            mapOf("target" to mapOf("type" to "string", "description" to "Terminal name"),
                  "path" to mapOf("type" to "string", "description" to "File path"),
                  "old_text" to mapOf("type" to "string", "description" to "Text to find"),
                  "new_text" to mapOf("type" to "string", "description" to "Replacement")),
            listOf("target", "path", "old_text", "new_text")),
        ToolDef("search_files", "Search for files by glob pattern on a remote terminal",
            mapOf("target" to mapOf("type" to "string", "description" to "Terminal name"),
                  "pattern" to mapOf("type" to "string", "description" to "Glob pattern"),
                  "path" to mapOf("type" to "string", "description" to "Base directory")),
            listOf("target", "pattern")),
        ToolDef("search_content", "Search file contents by regex on a remote terminal",
            mapOf("target" to mapOf("type" to "string", "description" to "Terminal name"),
                  "pattern" to mapOf("type" to "string", "description" to "Regex pattern"),
                  "path" to mapOf("type" to "string", "description" to "Directory")),
            listOf("target", "pattern")),
        ToolDef("clipboard_read", "Read clipboard from a remote terminal",
            mapOf("target" to mapOf("type" to "string", "description" to "Terminal name")),
            listOf("target")),
        ToolDef("clipboard_write", "Write text to clipboard on a remote terminal",
            mapOf("target" to mapOf("type" to "string", "description" to "Terminal name"),
                  "text" to mapOf("type" to "string", "description" to "Text to copy")),
            listOf("target", "text")),
        ToolDef("wake_device", "Wake a device and open an input box on it",
            mapOf("target" to mapOf("type" to "string", "description" to "Terminal name")),
            listOf("target")),
        // Phone-specific tools (work on Android terminals, including this phone)
        ToolDef("send_sms", "Send an SMS text message from an Android device",
            mapOf("target" to mapOf("type" to "string", "description" to "Terminal name (use 'local' for this phone)"),
                  "to" to mapOf("type" to "string", "description" to "Phone number"),
                  "message" to mapOf("type" to "string", "description" to "Message text")),
            listOf("target", "to", "message")),
        ToolDef("read_sms", "Read recent SMS messages from an Android device",
            mapOf("target" to mapOf("type" to "string", "description" to "Terminal name (use 'local' for this phone)"),
                  "limit" to mapOf("type" to "string", "description" to "Max messages to return (default 10)"),
                  "filter" to mapOf("type" to "string", "description" to "Filter: inbox, sent, or empty for all")),
            listOf("target")),
        ToolDef("make_call", "Make a phone call from an Android device",
            mapOf("target" to mapOf("type" to "string", "description" to "Terminal name (use 'local' for this phone)"),
                  "number" to mapOf("type" to "string", "description" to "Phone number to call")),
            listOf("target", "number")),
        ToolDef("set_alarm", "Set an alarm on an Android device. Use hour+minute for absolute time, or delay for relative.",
            mapOf("target" to mapOf("type" to "string", "description" to "Terminal name (use 'local' for this phone)"),
                  "hour" to mapOf("type" to "string", "description" to "Hour 0-23 (for absolute time)"),
                  "minute" to mapOf("type" to "string", "description" to "Minute 0-59 (for absolute time)"),
                  "delay" to mapOf("type" to "string", "description" to "Minutes from now (alternative to hour+minute)"),
                  "message" to mapOf("type" to "string", "description" to "Alarm label")),
            listOf("target")),
        ToolDef("get_location", "Get GPS location of an Android device",
            mapOf("target" to mapOf("type" to "string", "description" to "Terminal name (use 'local' for this phone)")),
            listOf("target")),
        ToolDef("read_contacts", "Search and read contacts on an Android device",
            mapOf("target" to mapOf("type" to "string", "description" to "Terminal name (use 'local' for this phone)"),
                  "search" to mapOf("type" to "string", "description" to "Name to search for (optional)"),
                  "limit" to mapOf("type" to "string", "description" to "Max results (default 20)")),
            listOf("target")),
        ToolDef("device_info", "Get device info: battery, storage, model, volume, etc.",
            mapOf("target" to mapOf("type" to "string", "description" to "Terminal name (use 'local' for this phone)")),
            listOf("target")),
        ToolDef("send_notification", "Show a notification on an Android device",
            mapOf("target" to mapOf("type" to "string", "description" to "Terminal name (use 'local' for this phone)"),
                  "title" to mapOf("type" to "string", "description" to "Notification title"),
                  "message" to mapOf("type" to "string", "description" to "Notification body")),
            listOf("target", "message")),
        ToolDef("tts", "Speak text aloud using text-to-speech on an Android device",
            mapOf("target" to mapOf("type" to "string", "description" to "Terminal name (use 'local' for this phone)"),
                  "text" to mapOf("type" to "string", "description" to "Text to speak"),
                  "lang" to mapOf("type" to "string", "description" to "Language code e.g. zh-CN, en-US (optional)")),
            listOf("target", "text")),
        ToolDef("vibrate", "Vibrate an Android device",
            mapOf("target" to mapOf("type" to "string", "description" to "Terminal name (use 'local' for this phone)"),
                  "duration" to mapOf("type" to "string", "description" to "Duration in ms (default 500)")),
            listOf("target")),
        ToolDef("set_volume", "Set volume level on an Android device",
            mapOf("target" to mapOf("type" to "string", "description" to "Terminal name (use 'local' for this phone)"),
                  "level" to mapOf("type" to "string", "description" to "Volume level (0 to max)"),
                  "stream" to mapOf("type" to "string", "description" to "Stream: music, ring, alarm, notification (default music)")),
            listOf("target", "level")),
        ToolDef("open_app", "Open an app on an Android device by package name",
            mapOf("target" to mapOf("type" to "string", "description" to "Terminal name (use 'local' for this phone)"),
                  "package" to mapOf("type" to "string", "description" to "Package name e.g. com.whatsapp")),
            listOf("target", "package")),
        // Memory tools
        ToolDef("save_memory", "Save a piece of information to persistent memory for future conversations",
            mapOf("name" to mapOf("type" to "string", "description" to "Short memory name (used as filename)"),
                  "content" to mapOf("type" to "string", "description" to "Content to remember")),
            listOf("name", "content")),
        ToolDef("recall_memory", "Search and recall saved memories",
            mapOf("search" to mapOf("type" to "string", "description" to "Search keyword (empty for all)")),
            emptyList()),
        ToolDef("delete_memory", "Delete a saved memory by name",
            mapOf("name" to mapOf("type" to "string", "description" to "Memory name to delete")),
            listOf("name")),
        // Nearby Transfer tools
        ToolDef("nearby_scan", "Scan for nearby Octo devices via BLE for P2P file transfer",
            mapOf("timeout" to mapOf("type" to "string", "description" to "Scan timeout in seconds (default 10)")),
            emptyList()),
        ToolDef("nearby_send", "Send a file to a nearby device via P2P Wi-Fi (no internet needed). Creates a local hotspot, waits for receiver to connect, then transfers the file.",
            mapOf("path" to mapOf("type" to "string", "description" to "File path to send")),
            listOf("path")),
        ToolDef("nearby_receive", "Receive a file from a nearby device via P2P Wi-Fi. Connects to the sender's BLE GATT to get hotspot info, joins the hotspot, then receives the file.",
            mapOf("sender_address" to mapOf("type" to "string", "description" to "BLE address of the sender device (from nearby_scan)"),
                  "save_dir" to mapOf("type" to "string", "description" to "Directory to save the file (default: /sdcard/Download)")),
            listOf("sender_address"))
    )

    private fun claudeTools(): JSONArray {
        val arr = JSONArray()
        for (t in toolDefs) {
            val props = JSONObject()
            for ((k, v) in t.properties) props.put(k, JSONObject(v))
            arr.put(JSONObject().apply {
                put("name", t.name)
                put("description", t.description)
                put("input_schema", JSONObject().apply {
                    put("type", "object")
                    put("properties", props)
                    put("required", JSONArray(t.required))
                })
            })
        }
        return arr
    }

    private fun openAiTools(): JSONArray {
        val arr = JSONArray()
        for (t in toolDefs) {
            val props = JSONObject()
            for ((k, v) in t.properties) props.put(k, JSONObject(v))
            arr.put(JSONObject().apply {
                put("type", "function")
                put("function", JSONObject().apply {
                    put("name", t.name)
                    put("description", t.description)
                    put("parameters", JSONObject().apply {
                        put("type", "object")
                        put("properties", props)
                        put("required", JSONArray(t.required))
                    })
                })
            })
        }
        return arr
    }

    // ---- Tool Execution ----

    /** Check if target refers to this phone (local execution). */
    private fun isLocal(target: String): Boolean {
        return target == "local" || target == config.terminalName
    }

    /** Execute a task locally via TaskHandler or remotely via relay. */
    private fun execTask(taskType: String, target: String, params: Map<String, Any>): String {
        if (isLocal(target) && localTaskHandler != null) {
            val taskJson = JSONObject().apply {
                put("type", taskType)
                for ((k, v) in params) put(k, v)
            }
            val (output, exitCode) = localTaskHandler.dispatch(taskJson)
            return if (exitCode != 0 && output.isEmpty()) "[failed with exit code $exitCode]" else output
        }
        return execSubmitAndWait(taskType, target, params)
    }

    /** Wait for user confirmation of a sensitive tool call. Returns true if approved. */
    private fun waitForConfirmation(toolName: String, args: Map<String, String>,
                                     onEvent: (Event) -> Unit): Boolean {
        val confirmId = "confirm-${System.currentTimeMillis()}"
        val desc = args.entries.joinToString("\n") { "${it.key}: ${it.value}" }
        confirmResults[confirmId] = null

        onEvent(Event.ConfirmRequired(confirmId, toolName, desc, args))

        // Block until user responds (timeout 60s)
        var waited = 0
        while (waited < 60000) {
            val result = confirmResults[confirmId]
            if (result != null) {
                confirmResults.remove(confirmId)
                return result
            }
            Thread.sleep(200)
            waited += 200
        }
        confirmResults.remove(confirmId)
        return false
    }

    /** Called by UI when user taps Allow/Deny. */
    fun onConfirmResponse(confirmId: String, approved: Boolean) {
        confirmResults[confirmId] = approved
    }

    // ---- Memory ----

    private fun execSaveMemory(args: JSONObject): String {
        val content = args.optString("content", "")
        val name = args.optString("name", "memory_${System.currentTimeMillis()}")
        if (content.isEmpty()) return "Missing 'content'"
        val file = java.io.File(memoryDir, "$name.md")
        file.writeText(content)
        return "Memory saved: $name"
    }

    private fun execRecallMemory(args: JSONObject): String {
        val search = args.optString("search", "")
        val files = memoryDir.listFiles { f -> f.extension == "md" } ?: return "No memories."
        if (files.isEmpty()) return "No memories."
        val results = files.mapNotNull { f ->
            val text = f.readText()
            if (search.isEmpty() || text.contains(search, ignoreCase = true) ||
                f.nameWithoutExtension.contains(search, ignoreCase = true)) {
                "## ${f.nameWithoutExtension}\n$text"
            } else null
        }
        return if (results.isEmpty()) "No matching memories." else results.joinToString("\n\n---\n\n")
    }

    private fun execDeleteMemory(args: JSONObject): String {
        val name = args.optString("name", "")
        if (name.isEmpty()) return "Missing 'name'"
        val file = java.io.File(memoryDir, "$name.md")
        return if (file.exists() && file.delete()) "Memory deleted: $name" else "Memory not found: $name"
    }

    // ---- Nearby Transfer ----

    private fun getOrCreateNearbyTransfer(): NearbyTransferManager {
        if (nearbyTransfer == null) {
            val ctx = appContext
                ?: throw IllegalStateException("No context for NearbyTransferManager")
            nearbyTransfer = NearbyTransferManager(ctx)
            nearbyTransfer!!.setDeviceName(config.terminalName.ifEmpty { "octo-phone" })
        }
        return nearbyTransfer!!
    }

    private fun execNearbyScan(args: JSONObject): String {
        val timeout = args.optString("timeout", "10").toLongOrNull() ?: 10
        return try {
            val mgr = getOrCreateNearbyTransfer()
            mgr.toolScan(timeout * 1000)
        } catch (e: Exception) {
            "Error: ${e.message}"
        }
    }

    private fun execNearbySend(args: JSONObject): String {
        val path = args.optString("path", "")
        if (path.isEmpty()) return "Error: missing 'path'"
        return try {
            val mgr = getOrCreateNearbyTransfer()
            mgr.toolSend(path)
        } catch (e: Exception) {
            "Error: ${e.message}"
        }
    }

    private fun execNearbyReceive(args: JSONObject): String {
        val address = args.optString("sender_address", "")
        if (address.isEmpty()) return "Error: missing 'sender_address'"
        val saveDir = args.optString("save_dir", "/sdcard/Download")
        return try {
            val mgr = getOrCreateNearbyTransfer()
            mgr.toolReceive(address, saveDir)
        } catch (e: Exception) {
            "Error: ${e.message}"
        }
    }

    private fun loadAgentMd(): String {
        return if (agentMdFile.exists()) agentMdFile.readText() else ""
    }

    private fun loadMemorySummary(): String {
        val files = memoryDir.listFiles { f -> f.extension == "md" } ?: return ""
        if (files.isEmpty()) return ""
        return files.joinToString("\n") { "- ${it.nameWithoutExtension}: ${it.readText().take(80)}" }
    }

    private fun executeTool(name: String, args: JSONObject, onEvent: ((Event) -> Unit)? = null): String {
        // Check confirmation for sensitive tools
        if (name in sensitiveTools && onEvent != null) {
            val argsMap = jsonToMap(args)
            if (!waitForConfirmation(name, argsMap, onEvent)) {
                return "User denied this action."
            }
        }
        return try {
            when (name) {
                "list_terminals" -> execListTerminals()
                "run_command" -> execTask("shell",
                    args.getString("target"), mapOf("command" to args.getString("command")))
                "read_file" -> execTask("cat",
                    args.getString("target"), mapOf("path" to args.getString("path")))
                "edit_file" -> execTask("edit",
                    args.getString("target"), mapOf(
                        "path" to args.getString("path"),
                        "old" to args.getString("old_text"),
                        "new" to args.getString("new_text")))
                "search_files" -> {
                    val p = mutableMapOf<String, Any>("pattern" to args.getString("pattern"))
                    if (args.optString("path", "").isNotEmpty()) p["path"] = args.getString("path")
                    execTask("glob", args.getString("target"), p)
                }
                "search_content" -> {
                    val p = mutableMapOf<String, Any>("pattern" to args.getString("pattern"))
                    if (args.optString("path", "").isNotEmpty()) p["path"] = args.getString("path")
                    execTask("grep", args.getString("target"), p)
                }
                "clipboard_read" -> execTask("clipboard_read",
                    args.getString("target"), emptyMap())
                "clipboard_write" -> execTask("clipboard_write",
                    args.getString("target"), mapOf("text" to args.getString("text")))
                "wake_device" -> {
                    relay.submitTask(args.getString("target"), "wake_input", emptyMap())
                    "Wake signal sent."
                }
                // Phone-specific tools
                "send_sms" -> execTask("send_sms",
                    args.getString("target"), mapOf("to" to args.getString("to"), "message" to args.getString("message")))
                "read_sms" -> execTask("read_sms",
                    args.getString("target"), mapOf(
                        "limit" to args.optString("limit", "10"),
                        "filter" to args.optString("filter", "")))
                "make_call" -> execTask("make_call",
                    args.getString("target"), mapOf("number" to args.getString("number")))
                "set_alarm" -> {
                    val p = mutableMapOf<String, Any>("message" to args.optString("message", "openOcto alarm"))
                    if (args.has("hour")) p["hour"] = args.optString("hour")
                    if (args.has("minute")) p["minute"] = args.optString("minute")
                    if (args.has("delay")) p["delay"] = args.optString("delay")
                    execTask("set_alarm", args.getString("target"), p)
                }
                "get_location" -> execTask("get_location",
                    args.getString("target"), emptyMap())
                "read_contacts" -> execTask("read_contacts",
                    args.getString("target"), mapOf(
                        "search" to args.optString("search", ""),
                        "limit" to args.optString("limit", "20")))
                "device_info" -> execTask("device_info",
                    args.getString("target"), emptyMap())
                "send_notification" -> execTask("send_notification",
                    args.getString("target"), mapOf(
                        "title" to args.optString("title", "openOcto"),
                        "message" to args.getString("message")))
                "tts" -> execTask("tts",
                    args.getString("target"), mapOf(
                        "text" to args.getString("text"),
                        "lang" to args.optString("lang", "")))
                "vibrate" -> execTask("vibrate",
                    args.getString("target"), mapOf(
                        "duration" to args.optString("duration", "500")))
                "set_volume" -> execTask("set_volume",
                    args.getString("target"), mapOf(
                        "level" to args.getString("level"),
                        "stream" to args.optString("stream", "music")))
                "open_app" -> execTask("open_app",
                    args.getString("target"), mapOf("package" to args.getString("package")))
                // Memory tools
                "save_memory" -> execSaveMemory(args)
                "recall_memory" -> execRecallMemory(args)
                "delete_memory" -> execDeleteMemory(args)
                // Nearby Transfer tools
                "nearby_scan" -> execNearbyScan(args)
                "nearby_send" -> execNearbySend(args)
                "nearby_receive" -> execNearbyReceive(args)
                else -> "Unknown tool: $name"
            }
        } catch (e: Exception) {
            "Error: ${e.message}"
        }
    }

    private fun execListTerminals(): String {
        val terminals = relay.listTerminals()
        if (terminals.isEmpty()) return "No terminals registered."
        val sb = StringBuilder()
        for (t in terminals) {
            val name = t.optString("name")
            val online = if (t.optBoolean("online")) "online" else "offline"
            val meta = t.optJSONObject("meta")
            val platform = meta?.optString("platform", "") ?: ""
            val shell = meta?.optString("shell", "") ?: ""
            sb.appendLine("$name [$online] platform=$platform shell=$shell")
        }
        return sb.toString()
    }

    private fun execSubmitAndWait(taskType: String, target: String, params: Map<String, Any>): String {
        val taskId = relay.submitTask(target, taskType, params)
        var elapsed = 0
        while (elapsed < TASK_TIMEOUT) {
            Thread.sleep(1000)
            elapsed++
            // Poll per-task key first, fallback to legacy
            val task = relay.pollTaskById(target, taskId)
                ?: relay.pollTask(target)
                ?: return "[Task disappeared]"
            val status = task.optString("status")
            if (status in listOf("DONE", "FAILED")) {
                val output = task.optString("output", "")
                val exitCode = task.optInt("exit_code", -1)
                relay.clearTaskById(target, taskId)
                relay.clearTask(target)  // also clear legacy key
                return if (exitCode != 0 && output.isEmpty()) "[exit code $exitCode]" else output
            }
        }
        return "[Timeout after ${TASK_TIMEOUT}s]"
    }

    private fun buildSystemPrompt(terminals: List<JSONObject>): String {
        val localName = config.terminalName.ifEmpty { "local" }
        val terminalInfo = if (terminals.isEmpty()) "No terminals registered." else
            terminals.joinToString("\n") { t ->
                val name = t.optString("name")
                val online = if (t.optBoolean("online")) "online" else "offline"
                val meta = t.optJSONObject("meta")
                val platform = meta?.optString("platform", "") ?: ""
                val shell = meta?.optString("shell", "") ?: ""
                "- $name ($online, $platform, $shell)"
            }

        val agentMd = loadAgentMd()
        val memorySummary = loadMemorySummary()
        val pet = currentPet

        val sb = StringBuilder()

        // Pet personality injection
        val petIntro = if (pet != null) {
            "${pet.personality}\nYou are running inside the openOcto app on an Android phone."
        } else {
            "You are an AI assistant running on an Android phone inside the openOcto app."
        }
        sb.appendLine("""$petIntro
You can control this phone AND any remote terminal in the network.

This phone's terminal name: "$localName" (you can also use "local" as target)

Available terminals:
$terminalInfo

Phone capabilities (use target="local" or "$localName"):
- send_sms / read_sms: Send and read SMS messages
- make_call: Make phone calls
- set_alarm: Set alarms (hour, minute, message)
- get_location: Get GPS coordinates
- read_contacts: Search and list contacts
- device_info: Battery, storage, model, volume info
- send_notification: Show a notification
- tts: Text-to-speech (speak text aloud)
- vibrate: Vibrate the phone
- set_volume: Adjust volume (music/ring/alarm/notification)
- open_app: Launch an app by package name
- run_command / read_file / edit_file: Also work locally on this phone

Remote terminal capabilities (use target=terminal_name):
- run_command / read_file / edit_file / search_files / search_content
- clipboard_read / clipboard_write / wake_device
- If the remote terminal is also an Android phone, phone capabilities work too

Nearby P2P Transfer (no internet, no router needed):
- nearby_scan: Scan for Octo devices nearby via BLE
- nearby_send: Send a file to a nearby device (creates local Wi-Fi hotspot, zero data cost)
- nearby_receive: Receive a file from a nearby sender
- Use this for large files between physically nearby devices. For small files or remote devices, use the relay.

Memory:
- save_memory: Persist useful info (user preferences, contacts, habits) for future conversations
- recall_memory: Search saved memories
- delete_memory: Remove a memory
- Save things the user tells you to remember, or facts that would be useful later.

File access level: "${config.fileAccessLevel}"
${when (config.fileAccessLevel) {
    "photos" -> "- Can only access: DCIM/, Pictures/. Shell commands are DISABLED."
    "documents" -> "- Can access: DCIM/, Pictures/, Documents/, Download/, .octo/. Shell commands are DISABLED."
    else -> "- Full file access. Shell commands are enabled."
}}

Guidelines:
- Sensitive actions (send_sms, make_call, set_alarm, open_app) require user confirmation.
- Use the correct shell syntax: bash for Linux/Mac, powershell for Windows, sh for Android.
- For file operations, prefer read_file/edit_file over running cat/sed commands.
- Be concise. Summarize command output rather than showing it raw.
- If a terminal is offline, tell the user.
- Respond in the same language as the user's message.""")

        if (agentMd.isNotEmpty()) {
            sb.appendLine("\n--- octo-agent.md ---\n$agentMd")
        }
        if (memorySummary.isNotEmpty()) {
            sb.appendLine("\n--- Saved Memories ---\n$memorySummary")
        }

        return sb.toString()
    }

    private fun jsonToMap(json: JSONObject): Map<String, String> {
        val map = mutableMapOf<String, String>()
        for (key in json.keys()) map[key] = json.optString(key, "")
        return map
    }
}
