package com.openocto.app

import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import org.json.JSONArray
import org.json.JSONObject
import java.util.concurrent.TimeUnit

/**
 * AI Agent that converts natural language to octo commands and executes them.
 * Supports Claude API and OpenAI-compatible APIs (GPT, Gemini, Ollama).
 */
class AiAgent(
    private val config: OctoConfig,
    private val relay: RelayClient,
    private val localTaskHandler: TaskHandler? = null
) {
    private val client = OkHttpClient.Builder()
        .connectTimeout(30, TimeUnit.SECONDS)
        .readTimeout(120, TimeUnit.SECONDS)
        .build()

    private val history = mutableListOf<JSONObject>()
    private val JSON_MEDIA = "application/json; charset=utf-8".toMediaType()
    private val MAX_TOOL_ROUNDS = 5
    private val TASK_TIMEOUT = 60 // seconds

    fun clearHistory() { history.clear() }

    /**
     * Process user message: send to AI, execute tool calls, return final response.
     * Runs on calling thread (must be background thread).
     */
    fun chat(userMessage: String, onStatus: (String) -> Unit): String {
        // Build system prompt with current terminal info
        val terminals = try { relay.listTerminals() } catch (_: Exception) { emptyList() }
        val systemPrompt = buildSystemPrompt(terminals)

        // Add user message
        if (isClaude()) {
            history.add(JSONObject().put("role", "user").put("content", userMessage))
        } else {
            history.add(JSONObject().put("role", "user").put("content", userMessage))
        }

        var rounds = MAX_TOOL_ROUNDS
        while (rounds > 0) {
            rounds--
            onStatus("Thinking...")

            val response = callLlm(systemPrompt)

            if (isClaude()) {
                val content = response.optJSONArray("content") ?: JSONArray()
                val stopReason = response.optString("stop_reason", "end_turn")

                // Collect text and tool_use blocks
                val textParts = mutableListOf<String>()
                val toolUses = mutableListOf<JSONObject>()
                for (i in 0 until content.length()) {
                    val block = content.getJSONObject(i)
                    when (block.optString("type")) {
                        "text" -> textParts.add(block.optString("text", ""))
                        "tool_use" -> toolUses.add(block)
                    }
                }

                if (toolUses.isEmpty() || stopReason == "end_turn") {
                    val text = textParts.joinToString("\n")
                    history.add(JSONObject().put("role", "assistant").put("content", content))
                    return text
                }

                // Add assistant message with tool calls
                history.add(JSONObject().put("role", "assistant").put("content", content))

                // Execute tools and add results
                val results = JSONArray()
                for (tu in toolUses) {
                    val toolName = tu.optString("name")
                    val toolId = tu.optString("id")
                    val input = tu.optJSONObject("input") ?: JSONObject()
                    onStatus("Running: $toolName...")
                    val result = executeTool(toolName, input)
                    results.put(JSONObject().apply {
                        put("type", "tool_result")
                        put("tool_use_id", toolId)
                        put("content", result)
                    })
                }
                history.add(JSONObject().put("role", "user").put("content", results))

            } else {
                // OpenAI format
                val choice = response.optJSONArray("choices")?.optJSONObject(0)
                    ?: return "Error: no response"
                val msg = choice.optJSONObject("message") ?: return "Error: no message"
                val toolCalls = msg.optJSONArray("tool_calls")

                if (toolCalls == null || toolCalls.length() == 0) {
                    val text = msg.optString("content", "")
                    history.add(msg)
                    return text
                }

                // Add assistant message
                history.add(msg)

                // Execute tool calls
                for (i in 0 until toolCalls.length()) {
                    val tc = toolCalls.getJSONObject(i)
                    val fn = tc.optJSONObject("function") ?: continue
                    val toolName = fn.optString("name")
                    val args = try { JSONObject(fn.optString("arguments", "{}")) } catch (_: Exception) { JSONObject() }
                    val callId = tc.optString("id")
                    onStatus("Running: $toolName...")
                    val result = executeTool(toolName, args)
                    history.add(JSONObject().apply {
                        put("role", "tool")
                        put("tool_call_id", callId)
                        put("content", result)
                    })
                }
            }
        }
        return "[Exceeded max tool rounds]"
    }

    // ---- LLM API Call ----

    private fun isClaude(): Boolean = config.aiProvider == "claude"

    private fun callLlm(systemPrompt: String): JSONObject {
        return if (isClaude()) callClaude(systemPrompt) else callOpenAi(systemPrompt)
    }

    private fun callClaude(systemPrompt: String): JSONObject {
        val baseUrl = config.aiBaseUrl.ifEmpty { "https://api.anthropic.com" }.trimEnd('/')
        val model = config.aiModel.ifEmpty { "claude-sonnet-4-20250514" }

        val body = JSONObject().apply {
            put("model", model)
            put("max_tokens", 4096)
            put("system", systemPrompt)
            put("tools", claudeTools())
            put("messages", JSONArray(history.map { it.toString() }.map { JSONObject(it) }))
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
            if (!resp.isSuccessful) throw Exception("API error ${resp.code}: $respBody")
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
            if (!resp.isSuccessful) throw Exception("API error ${resp.code}: $respBody")
            return JSONObject(respBody)
        }
    }

    // ---- Tool Definitions ----

    data class ToolDef(
        val name: String,
        val description: String,
        val properties: Map<String, Map<String, String>>,
        val required: List<String> = emptyList()
    )

    private val toolDefs = listOf(
        ToolDef("list_terminals",
            "List all registered terminals with their online/offline status, platform, and tags",
            emptyMap()),
        ToolDef("run_command",
            "Execute a shell command on a remote terminal. Use bash syntax for Linux/Mac, powershell for Windows.",
            mapOf(
                "target" to mapOf("type" to "string", "description" to "Terminal name"),
                "command" to mapOf("type" to "string", "description" to "Shell command to execute")
            ),
            listOf("target", "command")),
        ToolDef("read_file",
            "Read a file on a remote terminal with line numbers",
            mapOf(
                "target" to mapOf("type" to "string", "description" to "Terminal name"),
                "path" to mapOf("type" to "string", "description" to "File path on remote")
            ),
            listOf("target", "path")),
        ToolDef("edit_file",
            "Edit a file on a remote terminal by replacing text",
            mapOf(
                "target" to mapOf("type" to "string", "description" to "Terminal name"),
                "path" to mapOf("type" to "string", "description" to "File path"),
                "old_text" to mapOf("type" to "string", "description" to "Text to find"),
                "new_text" to mapOf("type" to "string", "description" to "Replacement text")
            ),
            listOf("target", "path", "old_text", "new_text")),
        ToolDef("search_files",
            "Search for files by glob pattern on a remote terminal",
            mapOf(
                "target" to mapOf("type" to "string", "description" to "Terminal name"),
                "pattern" to mapOf("type" to "string", "description" to "Glob pattern (e.g. **/*.py)"),
                "path" to mapOf("type" to "string", "description" to "Base directory (optional)")
            ),
            listOf("target", "pattern")),
        ToolDef("search_content",
            "Search file contents by regex on a remote terminal",
            mapOf(
                "target" to mapOf("type" to "string", "description" to "Terminal name"),
                "pattern" to mapOf("type" to "string", "description" to "Regex pattern"),
                "path" to mapOf("type" to "string", "description" to "Directory to search (optional)")
            ),
            listOf("target", "pattern")),
        ToolDef("clipboard_read",
            "Read clipboard content from a remote terminal",
            mapOf("target" to mapOf("type" to "string", "description" to "Terminal name")),
            listOf("target")),
        ToolDef("clipboard_write",
            "Write text to clipboard on a remote terminal",
            mapOf(
                "target" to mapOf("type" to "string", "description" to "Terminal name"),
                "text" to mapOf("type" to "string", "description" to "Text to copy")
            ),
            listOf("target", "text")),
        ToolDef("wake_device",
            "Wake a device and open an input box on it for the user to type. Use this when the user wants to type on another device.",
            mapOf("target" to mapOf("type" to "string", "description" to "Terminal name")),
            listOf("target")),
        // Phone-specific tools
        ToolDef("send_sms", "Send an SMS text message from an Android device",
            mapOf("target" to mapOf("type" to "string", "description" to "Terminal name (use 'local' for this phone)"),
                  "to" to mapOf("type" to "string", "description" to "Phone number"),
                  "message" to mapOf("type" to "string", "description" to "Message text")),
            listOf("target", "to", "message")),
        ToolDef("read_sms", "Read recent SMS messages from an Android device",
            mapOf("target" to mapOf("type" to "string", "description" to "Terminal name (use 'local' for this phone)"),
                  "limit" to mapOf("type" to "string", "description" to "Max messages (default 10)"),
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
                  "search" to mapOf("type" to "string", "description" to "Name to search (optional)"),
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
            listOf("target", "package"))
    )

    private fun claudeTools(): JSONArray {
        val arr = JSONArray()
        for (t in toolDefs) {
            val props = JSONObject()
            for ((k, v) in t.properties) {
                props.put(k, JSONObject(v))
            }
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
            for ((k, v) in t.properties) {
                props.put(k, JSONObject(v))
            }
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

    private fun isLocal(target: String): Boolean {
        return target == "local" || target == config.terminalName
    }

    private fun execTask(taskType: String, target: String, params: Map<String, Any>): String {
        if (isLocal(target) && localTaskHandler != null) {
            val taskJson = JSONObject().apply {
                put("type", taskType)
                for ((k, v) in params) put(k, v)
            }
            val (output, exitCode) = localTaskHandler.dispatch(taskJson)
            return if (exitCode != 0 && output.isEmpty()) "[failed with exit code $exitCode]" else output
        }
        val taskId = relay.submitTask(target, taskType, params)
        return waitForResult(target, taskId)
    }

    private fun executeTool(name: String, args: JSONObject): String {
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
            val tags = t.optJSONArray("tags")?.let { arr ->
                (0 until arr.length()).map { arr.getString(it) }.joinToString(", ")
            } ?: ""
            sb.appendLine("$name [$online] platform=$platform shell=$shell tags=$tags")
        }
        return sb.toString()
    }

    private fun waitForResult(target: String, taskId: String = ""): String {
        var elapsed = 0
        while (elapsed < TASK_TIMEOUT) {
            Thread.sleep(1000)
            elapsed++
            val task = (if (taskId.isNotEmpty()) relay.pollTaskById(target, taskId) else null)
                ?: relay.pollTask(target)
                ?: return "[Task disappeared]"
            val status = task.optString("status")
            if (status in listOf("DONE", "FAILED")) {
                val output = task.optString("output", "")
                val exitCode = task.optInt("exit_code", -1)
                if (taskId.isNotEmpty()) relay.clearTaskById(target, taskId)
                relay.clearTask(target)
                return if (exitCode != 0 && output.isEmpty()) "[exit code $exitCode]" else output
            }
        }
        return "[Timeout after ${TASK_TIMEOUT}s]"
    }

    // ---- System Prompt ----

    private fun buildSystemPrompt(terminals: List<JSONObject>): String {
        val localName = config.terminalName.ifEmpty { "local" }
        val terminalInfo = if (terminals.isEmpty()) {
            "No terminals registered."
        } else {
            terminals.joinToString("\n") { t ->
                val name = t.optString("name")
                val online = if (t.optBoolean("online")) "online" else "offline"
                val meta = t.optJSONObject("meta")
                val platform = meta?.optString("platform", "") ?: ""
                val shell = meta?.optString("shell", "") ?: ""
                "- $name ($online, $platform, $shell)"
            }
        }

        return """You are an AI assistant running on an Android phone inside the openOcto app.
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

Guidelines:
- Use the correct shell syntax: bash for Linux/Mac, powershell for Windows, sh for Android.
- For file operations, prefer read_file/edit_file over running cat/sed commands.
- Be concise. Summarize command output rather than showing it raw.
- If a terminal is offline, tell the user.
- Respond in the same language as the user's message."""
    }
}
