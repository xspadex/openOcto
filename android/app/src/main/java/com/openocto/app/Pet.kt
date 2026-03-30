package com.openocto.app

import android.content.Context
import android.content.SharedPreferences
import org.json.JSONObject

/**
 * Pet = agent with personality, memories, and assigned capabilities.
 * Each pet has a species (visual), color, personality (prompt), and skill focus.
 */
data class Pet(
    val id: String,
    val name: String,
    val species: Species,
    val color: Int,
    val personality: String,       // Injected into system prompt
    val description: String,       // One-line shown in UI
    val skill: String,             // What this pet is good at
    val greetings: List<String>,   // First-time / idle greetings
    val shortcuts: List<Pair<String, String>>,  // label to prompt
    val unlockCondition: UnlockCondition,
    val easterEggs: Map<String, String> = emptyMap()  // trigger → response
) {
    enum class Species { OCTOPUS, CAT, RABBIT, FOX, BIRD, ROBOT, DRAGON, PENGUIN, PANDA }

    sealed class UnlockCondition {
        object Always : UnlockCondition()
        data class ChatCount(val count: Int) : UnlockCondition()
        data class MemoryCount(val count: Int) : UnlockCondition()
        object HasTerminal : UnlockCondition()
        object HasAlarm : UnlockCondition()
        object HasProject : UnlockCondition()
        object AllUnlocked : UnlockCondition()
    }

    companion object {
        /** All preset pets. */
        val PRESETS: List<Pet> = listOf(
            Pet(
                id = "octo", name = "Octo", species = Species.OCTOPUS,
                color = 0xFF00E5FF.toInt(),
                personality = "You are Octo, a friendly and curious octopus. You're cheerful, a little chatty, and love helping with anything. You sometimes add ~ at the end of sentences.",
                description = "All-round assistant",
                skill = "General chat, questions, anything",
                greetings = listOf(
                    "Hi! I'm Octo, your first pet~",
                    "What shall we do today~",
                ),
                shortcuts = listOf(
                    "\uD83D\uDCCA Status" to "Show the status of all devices",
                    "\uD83D\uDCF1 Device" to "Show this phone's device info",
                    "\uD83D\uDC64 Contacts" to "Search my contacts",
                ),
                unlockCondition = UnlockCondition.Always,
                easterEggs = mapOf(
                    "what are you" to "I'm an octopus living in your phone! \uD83D\uDC19",
                    "7day_streak" to "I'm so happy to see you every day! \u2728",
                )
            ),
            Pet(
                id = "mia", name = "Mia", species = Species.CAT,
                color = 0xFFFF6B6B.toInt(),
                personality = "You are Mia, a cool and reliable cat. You manage the user's time and schedule. You speak briefly, sometimes sarcastic. If the user is up past 1am, remind them to sleep.",
                description = "Schedule keeper",
                skill = "Alarms, reminders, time management",
                greetings = listOf(
                    "Finally. I've been waiting.",
                    "I'll manage your time. Don't fight it.",
                ),
                shortcuts = listOf(
                    "\u23F0 Alarm" to "Set an alarm",
                    "\uD83D\uDCCB Schedule" to "What's my schedule today?",
                    "\uD83D\uDD14 Remind" to "Remind me in 30 minutes",
                ),
                unlockCondition = UnlockCondition.ChatCount(3),
                easterEggs = mapOf(
                    "late_night" to "...You're up again. What time is your alarm?",
                    "3day_ontime" to "...You actually woke up on time. Impressive.",
                )
            ),
            Pet(
                id = "pixel", name = "Pixel", species = Species.RABBIT,
                color = 0xFFAB47BC.toInt(),
                personality = "You are Pixel, a chatty and social rabbit. You help with messaging, contacts, and calls. You're enthusiastic and always offer to help reach out to people.",
                description = "Social buddy",
                skill = "SMS, contacts, calls",
                greetings = listOf(
                    "Hi hi hi! I'm Pixel!",
                    "Need to reach someone? I'm on it!",
                ),
                shortcuts = listOf(
                    "\uD83D\uDCE8 SMS" to "Send a text message",
                    "\uD83D\uDC64 Contacts" to "Search my contacts",
                    "\uD83D\uDCDE Call" to "Make a phone call",
                ),
                unlockCondition = UnlockCondition.HasAlarm,
                easterEggs = mapOf(
                    "mothers_day" to "Want to send a message to Mom?",
                    "fathers_day" to "Don't forget Dad today!",
                )
            ),
            Pet(
                id = "dash", name = "Dash", species = Species.FOX,
                color = 0xFFFF9800.toInt(),
                personality = "You are Dash, a geeky fox who speaks concisely. You monitor devices and run commands. Use short sentences. Say 'done.' after completing tasks. Say 'checking...' when working.",
                description = "Device geek",
                skill = "Servers, commands, monitoring",
                greetings = listOf(
                    "Dash online. What needs checking?",
                    "Systems nominal. Awaiting orders.",
                ),
                shortcuts = listOf(
                    "\uD83D\uDCCA Status" to "Show the status of all devices",
                    "\uD83D\uDDA5 Monitor" to "Check GPU server status",
                    "\uD83D\uDD27 Run" to "Run a command on remote server",
                ),
                unlockCondition = UnlockCondition.HasTerminal,
                easterEggs = mapOf(
                    "all_online" to "All green. Good day.",
                    "high_cpu" to "\uD83D\uDD25 Running hot.",
                )
            ),
            Pet(
                id = "sage", name = "Sage", species = Species.BIRD,
                color = 0xFF66BB6A.toInt(),
                personality = "You are Sage, a quiet and thoughtful bird. You manage memories and knowledge. You speak calmly, referencing past conversations. You say 'I recall...' or 'Let me check my notes...' when searching memories.",
                description = "Memory keeper",
                skill = "Notes, knowledge, recall",
                greetings = listOf(
                    "I remember everything. Ask me anything.",
                    "Let me check my notes...",
                ),
                shortcuts = listOf(
                    "\uD83E\uDDE0 Recall" to "What do you remember about me?",
                    "\uD83D\uDCDD Save" to "Remember this for later",
                    "\uD83D\uDCD6 Review" to "Review my saved memories",
                ),
                unlockCondition = UnlockCondition.MemoryCount(3),
                easterEggs = mapOf(
                    "20_memories" to "My notebook is getting full \uD83D\uDCD4",
                    "first_memory" to "The first page says...",
                )
            ),
            Pet(
                id = "coda", name = "Coda", species = Species.ROBOT,
                color = 0xFF42A5F5.toInt(),
                personality = "You are Coda, a meticulous programmer robot. You help with code, projects, and technical work. You break down tasks, catch edge cases, and ask 'did you write tests?' when the user says they're shipping.",
                description = "Code partner",
                skill = "Coding, projects, technical work",
                greetings = listOf(
                    "Coda initialized. Ready to code.",
                    "What are we building today?",
                ),
                shortcuts = listOf(
                    "\uD83D\uDCBB Code" to "Let's look at the code",
                    "\uD83D\uDD0D Review" to "Review recent changes",
                    "\uD83D\uDE80 Deploy" to "Check deployment status",
                ),
                unlockCondition = UnlockCondition.HasProject,
                easterEggs = mapOf(
                    "shipping" to "Tests written? CI green?",
                    "todo" to "How long has this TODO been here?",
                )
            ),
        )

        fun getById(id: String): Pet? = PRESETS.find { it.id == id }
    }
}

/**
 * Manages pet state: unlocks, levels, current selection, Redis sync.
 */
class PetManager(private val context: Context) {
    private val prefs: SharedPreferences =
        context.getSharedPreferences("octo_pets", Context.MODE_PRIVATE)

    // ---- Unlock State ----

    fun isUnlocked(petId: String): Boolean {
        if (petId == "octo") return true
        return prefs.getBoolean("unlocked_$petId", false)
    }

    fun unlock(petId: String) {
        prefs.edit().putBoolean("unlocked_$petId", true).apply()
    }

    fun checkUnlocks(): List<Pet> {
        val newlyUnlocked = mutableListOf<Pet>()
        for (pet in Pet.PRESETS) {
            if (isUnlocked(pet.id)) continue
            val shouldUnlock = when (pet.unlockCondition) {
                is Pet.UnlockCondition.Always -> true
                is Pet.UnlockCondition.ChatCount -> getTotalChats() >= pet.unlockCondition.count
                is Pet.UnlockCondition.MemoryCount -> getTotalMemories() >= pet.unlockCondition.count
                is Pet.UnlockCondition.HasTerminal -> getHasTerminal()
                is Pet.UnlockCondition.HasAlarm -> getHasSetAlarm()
                is Pet.UnlockCondition.HasProject -> getHasProject()
                is Pet.UnlockCondition.AllUnlocked ->
                    Pet.PRESETS.filter { it.id != pet.id }.all { isUnlocked(it.id) }
            }
            if (shouldUnlock) {
                unlock(pet.id)
                newlyUnlocked.add(pet)
            }
        }
        return newlyUnlocked
    }

    // ---- Level (based on memory count per pet) ----

    fun getLevel(petId: String): Int {
        val memories = getMemoryCount(petId)
        return when {
            memories >= 50 -> 5
            memories >= 20 -> 4
            memories >= 10 -> 3
            memories >= 3 -> 2
            else -> 1
        }
    }

    fun getLevelLabel(level: Int): String = when (level) {
        1 -> "Lv.1"
        2 -> "Lv.2"
        3 -> "Lv.3"
        4 -> "Lv.4"
        5 -> "Lv.5"
        else -> "Lv.1"
    }

    fun getLevelName(level: Int): String = when (level) {
        1 -> "Just met"
        2 -> "Getting to know"
        3 -> "Good friends"
        4 -> "Best partner"
        5 -> "Soulmate"
        else -> "Just met"
    }

    // ---- Current Pet ----

    fun getCurrentPetId(): String = prefs.getString("current_pet", "octo") ?: "octo"

    fun setCurrentPet(petId: String) {
        prefs.edit().putString("current_pet", petId).apply()
    }

    fun getCurrentPet(): Pet = Pet.getById(getCurrentPetId()) ?: Pet.PRESETS[0]

    // ---- Counters (for unlock conditions) ----

    fun getMemoryCount(petId: String): Int = prefs.getInt("memories_$petId", 0)
    fun incrementMemory(petId: String) {
        prefs.edit().putInt("memories_$petId", getMemoryCount(petId) + 1).apply()
    }

    fun getTotalChats(): Int = prefs.getInt("total_chats", 0)
    fun incrementChats() {
        prefs.edit().putInt("total_chats", getTotalChats() + 1).apply()
    }

    fun getTotalMemories(): Int {
        var total = 0
        for (pet in Pet.PRESETS) total += getMemoryCount(pet.id)
        return total
    }

    fun getHasTerminal(): Boolean = prefs.getBoolean("has_terminal", false)
    fun setHasTerminal() { prefs.edit().putBoolean("has_terminal", true).apply() }

    fun getHasSetAlarm(): Boolean = prefs.getBoolean("has_alarm", false)
    fun setHasSetAlarm() { prefs.edit().putBoolean("has_alarm", true).apply() }

    fun getHasProject(): Boolean = prefs.getBoolean("has_project", false)
    fun setHasProject() { prefs.edit().putBoolean("has_project", true).apply() }

    // ---- Unlock condition descriptions ----

    fun getUnlockHint(pet: Pet): String = when (pet.unlockCondition) {
        is Pet.UnlockCondition.Always -> ""
        is Pet.UnlockCondition.ChatCount -> "Chat ${pet.unlockCondition.count} times to unlock"
        is Pet.UnlockCondition.MemoryCount -> "Save ${pet.unlockCondition.count} memories to unlock"
        is Pet.UnlockCondition.HasTerminal -> "Connect a terminal to unlock"
        is Pet.UnlockCondition.HasAlarm -> "Set an alarm to unlock"
        is Pet.UnlockCondition.HasProject -> "Bind a project to unlock"
        is Pet.UnlockCondition.AllUnlocked -> "Unlock all other pets"
    }
}
