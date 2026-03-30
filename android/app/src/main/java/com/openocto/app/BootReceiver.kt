package com.openocto.app

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent

/**
 * Starts daemon on device boot if configured.
 */
class BootReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        if (intent.action == Intent.ACTION_BOOT_COMPLETED) {
            val config = OctoConfig(context)
            val prefs = context.getSharedPreferences("octo_config", Context.MODE_PRIVATE)
            val autoStart = prefs.getBoolean("auto_start", false)
            if (config.isConfigured && autoStart) {
                try {
                    val serviceIntent = Intent(context, DaemonService::class.java).apply {
                        action = DaemonService.ACTION_START
                    }
                    context.startForegroundService(serviceIntent)
                } catch (e: Exception) {
                    // Ignore - user can start manually
                }
            }
        }
    }
}
