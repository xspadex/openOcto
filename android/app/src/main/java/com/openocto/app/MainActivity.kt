package com.openocto.app

import android.Manifest
import android.app.AlarmManager
import android.app.AlertDialog
import android.content.Intent
import android.content.pm.PackageManager
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.os.Environment
import android.provider.Settings
import android.view.View
import android.widget.ArrayAdapter
import android.widget.EditText
import android.widget.LinearLayout
import android.widget.Spinner
import android.widget.TextView
import android.widget.Toast
import androidx.activity.result.contract.ActivityResultContracts
import androidx.appcompat.app.AppCompatActivity
import androidx.core.content.ContextCompat
import com.google.android.material.bottomnavigation.BottomNavigationView

/**
 * Main container — 2 tabs (Octo + Devices) + top bar with settings.
 */
class MainActivity : AppCompatActivity() {

    private lateinit var config: OctoConfig
    private var octoFragment: OctoFragment? = null
    private var devicesFragment: DevicesFragment? = null

    private val qrLauncher = registerForActivityResult(
        ActivityResultContracts.StartActivityForResult()
    ) { result ->
        if (result.resultCode == RESULT_OK) {
            val token = result.data?.getStringExtra("token") ?: return@registerForActivityResult
            if (config.importToken(token)) {
                Toast.makeText(this, "Connected!", Toast.LENGTH_SHORT).show()
                // Auto-set terminal name from device model
                if (config.terminalName.isEmpty()) {
                    config.terminalName = Build.MODEL.lowercase().replace(" ", "-").take(20)
                }
                // Refresh fragments
                octoFragment?.initEngine()
                devicesFragment?.onResume()
            } else {
                Toast.makeText(this, "Invalid token", Toast.LENGTH_SHORT).show()
            }
        }
    }

    private val permissionLauncher = registerForActivityResult(
        ActivityResultContracts.RequestMultiplePermissions()
    ) { _ -> }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_main)
        config = OctoConfig(this)

        // Top toolbar
        findViewById<com.google.android.material.appbar.MaterialToolbar>(R.id.toolbar).apply {
            // Left icon: open pet picker / history
            setNavigationOnClickListener { octoFragment?.showPetPicker() }

            // Tint menu icons to be visible on dark bg
            overflowIcon?.setTint(getColor(R.color.on_surface))
            menu.findItem(R.id.menu_scan_qr)?.icon?.setTint(getColor(R.color.primary))
            menu.findItem(R.id.menu_settings)?.icon?.setTint(getColor(R.color.on_surface))
            setOnMenuItemClickListener { item ->
                when (item.itemId) {
                    R.id.menu_scan_qr -> { scanQr(); true }
                    R.id.menu_settings -> { showSettings(); true }
                    else -> false
                }
            }
        }

        // Bottom navigation
        val bottomNav = findViewById<BottomNavigationView>(R.id.bottomNav)
        bottomNav.isItemActiveIndicatorEnabled = false
        bottomNav.setOnItemSelectedListener { item ->
            when (item.itemId) {
                R.id.nav_octo -> { showOctoTab(); true }
                R.id.nav_devices -> { showDevicesTab(); true }
                else -> false
            }
        }

        // Default to Octo tab
        showOctoTab()
    }

    private fun showOctoTab() {
        if (octoFragment == null) octoFragment = OctoFragment()
        supportFragmentManager.beginTransaction()
            .replace(R.id.fragmentContainer, octoFragment!!)
            .commit()
    }

    private fun showDevicesTab() {
        if (devicesFragment == null) devicesFragment = DevicesFragment()
        supportFragmentManager.beginTransaction()
            .replace(R.id.fragmentContainer, devicesFragment!!)
            .commit()
    }

    fun scanQr() {
        if (ContextCompat.checkSelfPermission(this, Manifest.permission.CAMERA)
            == PackageManager.PERMISSION_GRANTED) {
            qrLauncher.launch(Intent(this, QrScanActivity::class.java))
        } else {
            permissionLauncher.launch(arrayOf(Manifest.permission.CAMERA))
        }
    }

    // ---- Settings ----

    private fun showSettings() {
        val layout = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(dp(20), dp(12), dp(20), dp(0))
        }

        // AI Provider section
        addSettingsSection(layout, "\u2699 AI Provider",
            "${config.aiProvider.ifEmpty { "Not set" }} · ${config.aiModel.ifEmpty { "default" }}")
            .setOnClickListener { showApiSettings() }

        // Relay section
        val relayDesc = if (config.isConfigured) "Connected · ${config.workspace}" else "Not connected"
        addSettingsSection(layout, "\uD83D\uDCE1 Relay", relayDesc)
            .setOnClickListener { showRelaySettings() }

        // Terminal section
        addSettingsSection(layout, "\uD83D\uDCF1 Terminal",
            "${config.terminalName.ifEmpty { Build.MODEL }} · ${config.tags.ifEmpty { "no tags" }}")
            .setOnClickListener { showTerminalSettings() }

        // Divider
        layout.addView(View(this).apply {
            layoutParams = LinearLayout.LayoutParams(LinearLayout.LayoutParams.MATCH_PARENT, 1)
                .apply { topMargin = dp(12); bottomMargin = dp(4) }
            setBackgroundColor(getColor(R.color.outline))
        })

        // Octo Config
        addSettingsRow(layout, "\u2699 Octo Config") { showOctoConfigEditor() }
        addSettingsRow(layout, "\uD83E\uDDE0 ${getString(R.string.memories)}") { showMemories() }
        addSettingsRow(layout, "\uD83D\uDD12 ${getString(R.string.permissions)}") { showPermissionsDialog() }
        addSettingsRow(layout, "\uD83D\uDCC1 ${getString(R.string.file_access)}: ${config.fileAccessLevel}") { showPermissionsDialog() }

        // Language
        val langLabel = when (config.language) {
            "zh" -> "\uD83C\uDF10 ${getString(R.string.language)}: ${getString(R.string.lang_zh)}"
            "en" -> "\uD83C\uDF10 ${getString(R.string.language)}: English"
            else -> "\uD83C\uDF10 ${getString(R.string.language)}: ${getString(R.string.lang_system)}"
        }
        addSettingsRow(layout, langLabel) { showLanguagePicker() }

        // Divider
        layout.addView(View(this).apply {
            layoutParams = LinearLayout.LayoutParams(LinearLayout.LayoutParams.MATCH_PARENT, 1)
                .apply { topMargin = dp(8); bottomMargin = dp(4) }
            setBackgroundColor(getColor(R.color.outline))
        })

        // Clear Chat
        addSettingsRow(layout, "\uD83D\uDDD1 ${getString(R.string.clear_chat)}") { octoFragment?.clearChat() }

        // About
        layout.addView(TextView(this).apply {
            text = "\nv0.1.0 · Apache 2.0 · github.com/openocto"
            textSize = 11f
            setTextColor(0xFF888888.toInt())
            setPadding(0, dp(8), 0, dp(16))
        })

        AlertDialog.Builder(this)
            .setTitle(getString(R.string.settings))
            .setView(layout)
            .setNegativeButton(getString(R.string.close), null)
            .show()
    }

    private fun showLanguagePicker() {
        val codes = arrayOf("", "en", "zh")
        val labels = arrayOf(
            getString(R.string.lang_system),
            "English",
            getString(R.string.lang_zh)
        )
        val current = codes.indexOf(config.language).coerceAtLeast(0)
        AlertDialog.Builder(this)
            .setTitle(getString(R.string.language))
            .setSingleChoiceItems(labels, current) { dialog, which ->
                config.language = codes[which]
                OctoApp.applyLanguage(this)
                dialog.dismiss()
                recreate()
            }
            .setNegativeButton(getString(R.string.cancel), null)
            .show()
    }

    private fun addSettingsSection(parent: LinearLayout, title: String, subtitle: String): View {
        val row = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(dp(4), dp(14), dp(4), dp(14))
            isClickable = true
            isFocusable = true
            background = resources.getDrawable(android.R.drawable.list_selector_background, theme)
        }
        row.addView(TextView(this).apply {
            text = title; textSize = 16f; setTextColor(0xFF1A1A1A.toInt())
        })
        row.addView(TextView(this).apply {
            text = subtitle; textSize = 12f; setTextColor(0xFF666666.toInt())
            setPadding(0, dp(2), 0, 0)
        })
        parent.addView(row)
        return row
    }

    private fun addSettingsRow(parent: LinearLayout, title: String, onClick: () -> Unit) {
        val row = TextView(this).apply {
            text = title; textSize = 15f; setTextColor(0xFF1A1A1A.toInt())
            setPadding(dp(4), dp(14), dp(4), dp(14))
            isClickable = true
            setOnClickListener { onClick() }
        }
        parent.addView(row)
    }

    // ---- Sub-settings dialogs ----

    private fun showApiSettings() {
        val layout = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(dp(20), dp(16), dp(20), dp(8))
        }

        val providers = arrayOf("claude", "openai", "gemini", "ollama", "openrouter",
            "siliconflow", "qwen", "kimi", "minimax")
        val spinner = Spinner(this)
        spinner.adapter = ArrayAdapter(this, android.R.layout.simple_spinner_dropdown_item, providers)
        spinner.setSelection(providers.indexOf(config.aiProvider).coerceAtLeast(0))
        layout.addView(labeledView("Provider", spinner))

        val keyInput = EditText(this).apply {
            setText(config.aiApiKey); hint = "sk-..."
            inputType = android.text.InputType.TYPE_CLASS_TEXT or android.text.InputType.TYPE_TEXT_VARIATION_PASSWORD
            isSingleLine = true
        }
        layout.addView(labeledView("API Key", keyInput))

        val modelInput = EditText(this).apply {
            setText(config.aiModel); hint = "Leave empty for default"; isSingleLine = true
        }
        layout.addView(labeledView("Model (optional)", modelInput))

        val urlInput = EditText(this).apply {
            setText(config.aiBaseUrl); hint = "Leave empty for default"; isSingleLine = true
        }
        layout.addView(labeledView("Base URL (optional)", urlInput))

        AlertDialog.Builder(this)
            .setTitle("AI Provider")
            .setView(layout)
            .setPositiveButton("Save") { _, _ ->
                config.aiProvider = providers[spinner.selectedItemPosition]
                config.aiApiKey = keyInput.text.toString().trim()
                config.aiModel = modelInput.text.toString().trim()
                config.aiBaseUrl = urlInput.text.toString().trim()
                octoFragment?.initEngine()
                Toast.makeText(this, "Saved", Toast.LENGTH_SHORT).show()
            }
            .setNegativeButton("Cancel", null)
            .show()
    }

    private fun showRelaySettings() {
        val layout = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(dp(20), dp(16), dp(20), dp(8))
        }
        layout.addView(TextView(this).apply {
            text = if (config.isConfigured) "Connected to relay.\nScan a new QR code to change."
                   else "Not connected.\nScan a QR code to connect."
            textSize = 14f; setTextColor(getColor(R.color.on_surface_variant))
        })

        AlertDialog.Builder(this)
            .setTitle("Relay Connection")
            .setView(layout)
            .setPositiveButton("Scan QR") { _, _ -> scanQr() }
            .setNegativeButton("Close", null)
            .show()
    }

    private fun showTerminalSettings() {
        val layout = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(dp(20), dp(16), dp(20), dp(8))
        }
        val nameInput = EditText(this).apply {
            setText(config.terminalName.ifEmpty { Build.MODEL.lowercase().replace(" ", "-") })
            isSingleLine = true
        }
        layout.addView(labeledView("Terminal Name", nameInput))

        val tagsInput = EditText(this).apply {
            setText(config.tags); hint = "phone,android"; isSingleLine = true
        }
        layout.addView(labeledView("Tags", tagsInput))

        AlertDialog.Builder(this)
            .setTitle("Terminal")
            .setView(layout)
            .setPositiveButton("Save") { _, _ ->
                config.terminalName = nameInput.text.toString().trim()
                config.tags = tagsInput.text.toString().trim()
                Toast.makeText(this, "Saved", Toast.LENGTH_SHORT).show()
            }
            .setNegativeButton("Cancel", null)
            .show()
    }

    private fun showPermissionsDialog() {
        val layout = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(dp(20), dp(16), dp(20), dp(8))
        }

        data class PermItem(val name: String, val permission: String, val isSpecial: Boolean = false)
        val perms = listOf(
            PermItem("Camera", Manifest.permission.CAMERA),
            PermItem("Phone", Manifest.permission.CALL_PHONE),
            PermItem("SMS", Manifest.permission.SEND_SMS),
            PermItem("Contacts", Manifest.permission.READ_CONTACTS),
            PermItem("Location", Manifest.permission.ACCESS_FINE_LOCATION),
            PermItem("Mic", Manifest.permission.RECORD_AUDIO),
        )
        for (p in perms) {
            val granted = ContextCompat.checkSelfPermission(this, p.permission) == PackageManager.PERMISSION_GRANTED
            val row = LinearLayout(this).apply {
                orientation = LinearLayout.HORIZONTAL; gravity = android.view.Gravity.CENTER_VERTICAL
                setPadding(0, dp(8), 0, dp(8))
            }
            row.addView(View(this).apply {
                layoutParams = LinearLayout.LayoutParams(dp(10), dp(10)).apply { marginEnd = dp(12) }
                setBackgroundResource(if (granted) R.drawable.bg_status_dot_online else R.drawable.bg_status_dot_offline)
            })
            row.addView(TextView(this).apply {
                text = p.name; textSize = 14f; layoutParams = LinearLayout.LayoutParams(0, LinearLayout.LayoutParams.WRAP_CONTENT, 1f)
            })
            row.addView(TextView(this).apply {
                text = if (granted) "OK" else "—"; textSize = 12f
                setTextColor(if (granted) getColor(R.color.status_online) else getColor(R.color.status_offline))
            })
            layout.addView(row)
        }

        // File access level
        layout.addView(View(this).apply {
            layoutParams = LinearLayout.LayoutParams(LinearLayout.LayoutParams.MATCH_PARENT, 1)
                .apply { topMargin = dp(12); bottomMargin = dp(8) }
            setBackgroundColor(getColor(R.color.outline))
        })
        val levels = arrayOf("photos", "documents", "full")
        val levelLabels = arrayOf("Photos Only", "Documents", "Full Access")
        val spinner = Spinner(this)
        spinner.adapter = ArrayAdapter(this, android.R.layout.simple_spinner_dropdown_item, levelLabels)
        spinner.setSelection(levels.indexOf(config.fileAccessLevel).coerceAtLeast(0))
        layout.addView(labeledView("File Access", spinner))

        AlertDialog.Builder(this)
            .setTitle("Permissions")
            .setView(layout)
            .setPositiveButton("Save") { _, _ ->
                config.fileAccessLevel = levels[spinner.selectedItemPosition]
            }
            .setNeutralButton("Grant All") { _, _ ->
                val needed = perms.filter {
                    ContextCompat.checkSelfPermission(this, it.permission) != PackageManager.PERMISSION_GRANTED
                }.map { it.permission }
                if (needed.isNotEmpty()) permissionLauncher.launch(needed.toTypedArray())
            }
            .setNegativeButton("Close", null)
            .show()
    }

    private fun showOctoConfigEditor() {
        val agentFile = java.io.File("/sdcard/.octo/octo-agent.md")
        val content = try { if (agentFile.exists()) agentFile.readText() else "" } catch (_: Exception) { "" }
        val editText = EditText(this).apply {
            setText(content.ifEmpty {
                "# octo-agent\nname: ${Build.MODEL}\nrole: Personal assistant\n\n## Rules\n- Respond in Chinese\n\n## Shortcuts\n- GPU: Check nvidia-smi on gpu-server\n"
            })
            textSize = 13f; setTypeface(android.graphics.Typeface.MONOSPACE)
            gravity = android.view.Gravity.TOP; minLines = 10
            inputType = android.text.InputType.TYPE_CLASS_TEXT or
                    android.text.InputType.TYPE_TEXT_FLAG_MULTI_LINE or
                    android.text.InputType.TYPE_TEXT_FLAG_NO_SUGGESTIONS
            setBackgroundResource(R.drawable.bg_terminal_output)
            setTextColor(0xFFC9D1D9.toInt())
            setPadding(dp(12), dp(12), dp(12), dp(12))
        }
        AlertDialog.Builder(this)
            .setTitle("Octo Config")
            .setView(editText)
            .setPositiveButton("Save") { _, _ ->
                try {
                    agentFile.parentFile?.mkdirs()
                    agentFile.writeText(editText.text.toString())
                    Toast.makeText(this, "Saved", Toast.LENGTH_SHORT).show()
                } catch (e: Exception) {
                    Toast.makeText(this, "Failed: ${e.message}", Toast.LENGTH_SHORT).show()
                }
            }
            .setNegativeButton("Cancel", null)
            .show()
    }

    private fun showMemories() {
        val memDir = java.io.File("/sdcard/.octo/memories")
        val files = (memDir.listFiles() ?: emptyArray()).filter { it.extension == "md" }
            .sortedByDescending { it.lastModified() }

        if (files.isEmpty()) {
            Toast.makeText(this, "No memories yet", Toast.LENGTH_SHORT).show()
            return
        }

        val labels = files.map { "\uD83E\uDDE0 ${it.nameWithoutExtension}" }.toTypedArray()
        AlertDialog.Builder(this)
            .setTitle("Memories (${files.size})")
            .setItems(labels) { _, which ->
                val f = files[which]
                AlertDialog.Builder(this)
                    .setTitle(f.nameWithoutExtension)
                    .setMessage(f.readText())
                    .setPositiveButton("OK", null)
                    .setNegativeButton("Delete") { _, _ ->
                        f.delete()
                        Toast.makeText(this, "Deleted", Toast.LENGTH_SHORT).show()
                    }
                    .show()
            }
            .setNegativeButton("Close", null)
            .show()
    }

    // ---- Helpers ----

    private fun labeledView(label: String, view: View): LinearLayout {
        return LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            addView(TextView(context).apply {
                text = label; textSize = 12f; setTextColor(getColor(R.color.on_surface_variant))
                setPadding(0, dp(8), 0, dp(4))
            })
            addView(view)
        }
    }

    fun updateToolbarTitle(title: String) {
        findViewById<com.google.android.material.appbar.MaterialToolbar>(R.id.toolbar)
            .title = "  $title"
    }

    private fun dp(value: Int): Int = (value * resources.displayMetrics.density).toInt()
}
