package com.openocto.app

import android.app.NotificationManager
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent

/**
 * Handles alarm dismiss action from notification.
 */
class AlarmReceiver : BroadcastReceiver() {
    companion object {
        const val EXTRA_MESSAGE = "alarm_message"
        const val ACTION_DISMISS = "com.openocto.DISMISS_ALARM"
    }

    override fun onReceive(context: Context, intent: Intent) {
        if (intent.action == ACTION_DISMISS) {
            // Stop sound
            TaskHandler.fireAlarmStop()
            // Cancel notification
            val nm = context.getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager
            nm.cancel(9999)
        }
    }
}
