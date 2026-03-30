package com.openocto.app

import android.app.Application
import android.app.NotificationChannel
import android.app.NotificationManager
import android.os.Build
import androidx.appcompat.app.AppCompatDelegate
import androidx.core.os.LocaleListCompat

class OctoApp : Application() {
    companion object {
        const val CHANNEL_ID = "octo_daemon"
        const val ALARM_CHANNEL_ID = "octo_alarm"

        /** Apply saved language preference. Call from Application.onCreate and after user changes setting. */
        fun applyLanguage(context: android.content.Context) {
            val lang = OctoConfig(context).language
            if (lang.isNotEmpty()) {
                AppCompatDelegate.setApplicationLocales(
                    LocaleListCompat.forLanguageTags(lang)
                )
            } else {
                // Follow system
                AppCompatDelegate.setApplicationLocales(LocaleListCompat.getEmptyLocaleList())
            }
        }
    }

    override fun onCreate() {
        super.onCreate()
        applyLanguage(this)
        createNotificationChannels()
    }

    private fun createNotificationChannels() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            val daemonChannel = NotificationChannel(
                CHANNEL_ID,
                getString(R.string.channel_name),
                NotificationManager.IMPORTANCE_LOW
            ).apply {
                description = getString(R.string.channel_description)
            }

            val alarmChannel = NotificationChannel(
                ALARM_CHANNEL_ID,
                "Alarms",
                NotificationManager.IMPORTANCE_HIGH
            ).apply {
                description = "openOcto alarm notifications"
                enableVibration(true)
                setSound(
                    android.provider.Settings.System.DEFAULT_ALARM_ALERT_URI,
                    android.media.AudioAttributes.Builder()
                        .setUsage(android.media.AudioAttributes.USAGE_ALARM)
                        .build()
                )
            }

            val manager = getSystemService(NotificationManager::class.java)
            manager.createNotificationChannel(daemonChannel)
            manager.createNotificationChannel(alarmChannel)
        }
    }
}
