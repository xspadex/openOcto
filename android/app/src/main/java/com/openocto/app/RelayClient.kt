package com.openocto.app

import okhttp3.OkHttpClient
import okhttp3.Request
import org.json.JSONArray
import org.json.JSONObject
import java.net.URLEncoder
import java.util.concurrent.TimeUnit

/**
 * Upstash Redis REST API client - mirrors Python relay.py exactly.
 * Supports optional CF Worker proxy as fallback.
 */
class RelayClient(
    private val redisUrl: String,
    private val redisToken: String,
    private val workspace: String = "default",
    private val proxyUrl: String = ""
) {
    private val client = OkHttpClient.Builder()
        .connectTimeout(15, TimeUnit.SECONDS)
        .readTimeout(15, TimeUnit.SECONDS)
        .writeTimeout(15, TimeUnit.SECONDS)
        .build()

    private var useProxy = false // sticky fallback within session

    private val hasDirect get() = redisUrl.isNotEmpty() && redisToken.isNotEmpty()
    private val hasProxy get() = proxyUrl.isNotEmpty()

    fun key(vararg parts: String): String {
        return (listOf("octo", workspace) + parts).joinToString(":")
    }

    private fun doRequest(url: String, headers: Map<String, String>): String? {
        val builder = Request.Builder().url(url).get()
        for ((k, v) in headers) builder.addHeader(k, v)
        val request = builder.build()

        client.newCall(request).execute().use { response ->
            val body = response.body?.string() ?: return null
            if (!response.isSuccessful) throw Exception("HTTP ${response.code}: $body")
            val json = JSONObject(body)
            val result = json.opt("result")
            return when {
                result == null || result == JSONObject.NULL -> null
                result is JSONArray -> result.toString()
                else -> result.toString()
            }
        }
    }

    fun request(vararg args: String): String? {
        val path = "/" + args.joinToString("/") { URLEncoder.encode(it, "UTF-8") }

        // Determine order
        data class Target(val name: String, val baseUrl: String, val headers: Map<String, String>)

        val targets = mutableListOf<Target>()
        if (useProxy && hasProxy) {
            targets.add(Target("proxy", proxyUrl.trimEnd('/'),
                mapOf("X-Octo-Workspace" to workspace)))
            if (hasDirect) targets.add(Target("direct", redisUrl.trimEnd('/'),
                mapOf("Authorization" to "Bearer $redisToken")))
        } else if (hasDirect) {
            targets.add(Target("direct", redisUrl.trimEnd('/'),
                mapOf("Authorization" to "Bearer $redisToken")))
            if (hasProxy) targets.add(Target("proxy", proxyUrl.trimEnd('/'),
                mapOf("X-Octo-Workspace" to workspace)))
        } else if (hasProxy) {
            targets.add(Target("proxy", proxyUrl.trimEnd('/'),
                mapOf("X-Octo-Workspace" to workspace)))
        } else {
            throw Exception("No relay configured")
        }

        var lastError: Exception? = null
        for (target in targets) {
            repeat(3) { attempt ->
                try {
                    val result = doRequest(target.baseUrl + path, target.headers)
                    if (target.name == "proxy" && !useProxy) useProxy = true
                    return result
                } catch (e: Exception) {
                    lastError = e
                    if (attempt < 2) Thread.sleep(1000)
                }
            }
        }
        throw lastError ?: Exception("Connection failed")
    }

    // ---- Terminal Registration ----

    fun register(name: String, tags: List<String>, meta: Map<String, Any>) {
        val info = JSONObject().apply {
            put("tags", JSONArray(tags))
            put("meta", JSONObject(meta))
            put("registered_at", System.currentTimeMillis() / 1000)
            put("last_heartbeat", System.currentTimeMillis() / 1000)
        }
        request("HSET", key("terminals"), name, info.toString())
    }

    fun heartbeat(name: String) {
        val raw = request("HGET", key("terminals"), name) ?: return
        val info = JSONObject(raw)
        info.put("last_heartbeat", System.currentTimeMillis() / 1000)
        request("HSET", key("terminals"), name, info.toString())
    }

    fun unregister(name: String) {
        request("HDEL", key("terminals"), name)
        request("DEL", key("task", name))
    }

    fun listTerminals(): List<JSONObject> {
        val raw = request("HGETALL", key("terminals")) ?: return emptyList()
        val arr = JSONArray(raw)
        val terminals = mutableListOf<JSONObject>()
        var i = 0
        while (i < arr.length() - 1) {
            val name = arr.getString(i)
            val info = JSONObject(arr.getString(i + 1))
            val elapsed = (System.currentTimeMillis() / 1000) - info.optLong("last_heartbeat", 0)
            info.put("name", name)
            info.put("online", elapsed < 90)
            info.put("last_seen_ago", elapsed)
            terminals.add(info)
            i += 2
        }
        return terminals
    }

    // ---- Task Management (v2: parallel queue + per-task keys) ----

    private fun taskKey(target: String, taskId: String) = key("task", target, taskId)
    private fun queueKey(target: String) = key("queue", target)

    fun submitTask(target: String, taskType: String, params: Map<String, Any> = emptyMap()): String {
        val taskId = "${System.currentTimeMillis() / 1000}-${java.util.UUID.randomUUID().toString().take(8)}"
        val task = JSONObject().apply {
            put("id", taskId)
            put("type", taskType)
            put("status", "PENDING")
            put("output", "")
            put("exit_code", JSONObject.NULL)
            put("created_at", System.currentTimeMillis() / 1000)
            for ((k, v) in params) put(k, v)
        }
        // Per-task key with TTL
        request("SET", taskKey(target, taskId), task.toString())
        request("EXPIRE", taskKey(target, taskId), "3600")
        // Push to queue
        request("LPUSH", queueKey(target), taskId)
        // Legacy single-task key (backward compat)
        request("SET", key("task", target), task.toString())
        // Wake target
        setMode(target, "wake")
        return taskId
    }

    /** Poll a specific task by ID. */
    fun pollTaskById(target: String, taskId: String): JSONObject? {
        val raw = request("GET", taskKey(target, taskId)) ?: return null
        return try { JSONObject(raw) } catch (e: Exception) { null }
    }

    /** Poll legacy single-task key. */
    fun pollTask(name: String): JSONObject? {
        val raw = request("GET", key("task", name)) ?: return null
        return try { JSONObject(raw) } catch (e: Exception) { null }
    }

    fun updateTask(name: String, updates: Map<String, Any>) {
        val task = pollTask(name) ?: return
        updates.forEach { (k, v) -> task.put(k, v) }
        request("SET", key("task", name), task.toString())
    }

    fun completeTask(name: String, output: String, exitCode: Int) {
        updateTask(name, mapOf(
            "status" to "DONE",
            "output" to output,
            "exit_code" to exitCode
        ))
    }

    fun clearTask(name: String) {
        request("DEL", key("task", name))
    }

    fun clearTaskById(target: String, taskId: String) {
        request("DEL", taskKey(target, taskId))
    }

    // ---- Mode ----

    fun setMode(name: String, mode: String) {
        request("HSET", key("modes"), name, mode)
    }

    fun getMode(name: String): String {
        val result = request("HGET", key("modes"), name)
        return if (result in listOf("wake", "cool")) result!! else "cool"
    }

    // ---- Activity Feed ----

    fun pushFeed(entry: JSONObject) {
        if (!entry.has("ts")) entry.put("ts", System.currentTimeMillis() / 1000)
        request("LPUSH", key("feed"), entry.toString())
        request("LTRIM", key("feed"), "0", "99")
    }

    fun getFeed(count: Int = 20): List<JSONObject> {
        val raw = request("LRANGE", key("feed"), "0", (count - 1).toString()) ?: return emptyList()
        val arr = JSONArray(raw)
        return (0 until arr.length()).map { JSONObject(arr.getString(it)) }
    }

    // ---- Live Session ----

    fun getSession(name: String): Pair<String, JSONObject> {
        val output = request("GET", key("session", name, "output")) ?: ""
        val metaRaw = request("GET", key("session", name, "meta")) ?: "{}"
        val meta = try { JSONObject(metaRaw) } catch (_: Exception) { JSONObject() }
        return Pair(output, meta)
    }

    fun sendSessionInput(name: String, text: String) {
        request("LPUSH", key("session", name, "input"), text)
        request("EXPIRE", key("session", name, "input"), "300")
    }

    /**
     * Get all active sessions from the global sessions hash.
     * Returns list of (name, meta) pairs.
     */
    fun getActiveSessions(): List<Pair<String, JSONObject>> {
        val results = mutableListOf<Pair<String, JSONObject>>()
        try {
            val raw = request("HGETALL", key("sessions")) ?: return results
            val arr = JSONArray(raw)
            // HGETALL returns [key1, val1, key2, val2, ...]
            var i = 0
            while (i + 1 < arr.length()) {
                val name = arr.getString(i)
                val metaStr = arr.getString(i + 1)
                try {
                    val meta = JSONObject(metaStr)
                    if (meta.optString("status") == "active") {
                        results.add(Pair(name, meta))
                    }
                } catch (_: Exception) {}
                i += 2
            }
        } catch (_: Exception) {}
        return results
    }

    // ---- Notifications ----

    fun popNotifications(target: String, count: Int = 10): List<JSONObject> {
        val results = mutableListOf<JSONObject>()
        val k = key("notifications", target)
        for (i in 0 until count) {
            val raw = request("RPOP", k) ?: break
            try {
                val s = if (raw.startsWith("\"")) JSONObject(org.json.JSONTokener(raw)).toString()
                    else raw
                if (s.trimStart().startsWith("{")) results.add(JSONObject(s))
            } catch (_: Exception) { break }
        }
        return results
    }

    // ---- Inbox ----

    fun submitInboxTask(target: String, filename: String, sender: String,
                        note: String, url: String = "", redisKey: String = "") {
        val taskId = "${System.currentTimeMillis() / 1000}-${(Math.random() * 10000).toInt()}"
        val task = JSONObject().apply {
            put("id", taskId)
            put("type", "inbox_receive")
            put("status", "PENDING")
            put("output", "")
            put("exit_code", JSONObject.NULL)
            put("created_at", System.currentTimeMillis() / 1000)
            put("filename", filename)
            put("sender", sender)
            put("note", note)
            if (url.isNotEmpty()) put("url", url)
            if (redisKey.isNotEmpty()) put("redis_key", redisKey)
        }
        request("SET", key("task", target), task.toString())
        setMode(target, "wake")
    }

    // ---- Pet Context Sync ----

    fun pushPetContext(petId: String, data: JSONObject) {
        request("SET", key("pets", petId), data.toString())
    }

    fun pullPetContext(petId: String): JSONObject? {
        val raw = request("GET", key("pets", petId)) ?: return null
        return try { JSONObject(raw) } catch (_: Exception) { null }
    }

    fun listPets(): List<String> {
        val raw = request("KEYS", key("pets", "*")) ?: return emptyList()
        val arr = JSONArray(raw)
        return (0 until arr.length()).map {
            arr.getString(it).substringAfterLast(":")
        }
    }

    fun pushPetMemory(petId: String, name: String, content: String) {
        request("HSET", key("pets", petId, "memories"), name, content)
    }

    fun pullPetMemories(petId: String): Map<String, String> {
        val raw = request("HGETALL", key("pets", petId, "memories")) ?: return emptyMap()
        val arr = JSONArray(raw)
        val map = mutableMapOf<String, String>()
        var i = 0
        while (i < arr.length() - 1) {
            map[arr.getString(i)] = arr.getString(i + 1)
            i += 2
        }
        return map
    }
}
