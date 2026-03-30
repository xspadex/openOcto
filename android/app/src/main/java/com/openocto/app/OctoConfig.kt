package com.openocto.app

import android.content.Context
import android.content.SharedPreferences
import android.util.Base64
import org.json.JSONObject

/**
 * Manages openOcto configuration (Redis relay credentials, terminal settings, AI config).
 */
class OctoConfig(context: Context) {
    private val prefs: SharedPreferences =
        context.getSharedPreferences("octo_config", Context.MODE_PRIVATE)

    var redisUrl: String
        get() = prefs.getString("redis_url", "") ?: ""
        set(value) = prefs.edit().putString("redis_url", value).apply()

    var redisToken: String
        get() = prefs.getString("redis_token", "") ?: ""
        set(value) = prefs.edit().putString("redis_token", value).apply()

    var workspace: String
        get() = prefs.getString("workspace", "default") ?: "default"
        set(value) = prefs.edit().putString("workspace", value).apply()

    var proxyUrl: String
        get() = prefs.getString("proxy_url", "") ?: ""
        set(value) = prefs.edit().putString("proxy_url", value).apply()

    var terminalName: String
        get() = prefs.getString("terminal_name", "") ?: ""
        set(value) = prefs.edit().putString("terminal_name", value).apply()

    var tags: String
        get() = prefs.getString("tags", "") ?: ""
        set(value) = prefs.edit().putString("tags", value).apply()

    // AI configuration
    var aiProvider: String
        get() = prefs.getString("ai_provider", "") ?: ""
        set(value) = prefs.edit().putString("ai_provider", value).apply()

    var aiApiKey: String
        get() = prefs.getString("ai_api_key", "") ?: ""
        set(value) = prefs.edit().putString("ai_api_key", value).apply()

    var aiModel: String
        get() = prefs.getString("ai_model", "") ?: ""
        set(value) = prefs.edit().putString("ai_model", value).apply()

    var aiBaseUrl: String
        get() = prefs.getString("ai_base_url", "") ?: ""
        set(value) = prefs.edit().putString("ai_base_url", value).apply()

    var preferredAgent: String
        get() = prefs.getString("preferred_agent", "") ?: ""
        set(value) = prefs.edit().putString("preferred_agent", value).apply()

    /** File access level: "photos", "documents", "full" */
    var fileAccessLevel: String
        get() = prefs.getString("file_access_level", "documents") ?: "documents"
        set(value) = prefs.edit().putString("file_access_level", value).apply()

    /** Language: "" = follow system, "en" = English, "zh" = Chinese */
    var language: String
        get() = prefs.getString("language", "") ?: ""
        set(value) = prefs.edit().putString("language", value).apply()

    val isConfigured: Boolean
        get() = (redisUrl.isNotEmpty() && redisToken.isNotEmpty() || proxyUrl.isNotEmpty())
                && terminalName.isNotEmpty()

    val isAiConfigured: Boolean
        get() = aiApiKey.isNotEmpty() && aiProvider.isNotEmpty()

    fun importToken(token: String): Boolean {
        if (!token.startsWith("octo://")) return false
        return try {
            val b64 = token.removePrefix("octo://")
            val padded = b64 + "=".repeat((4 - b64.length % 4) % 4)
            val json = String(Base64.decode(padded, Base64.URL_SAFE))
            val obj = JSONObject(json)
            redisUrl = obj.optString("u", "")
            redisToken = obj.optString("t", "")
            workspace = obj.optString("w", "default")
            if (obj.has("p")) proxyUrl = obj.getString("p")
            true
        } catch (e: Exception) {
            false
        }
    }
}
