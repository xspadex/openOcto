package com.openocto.app

import android.text.SpannableStringBuilder

/**
 * Generates ASCII art for each pet species using plain text characters.
 */
object PetPixelArt {

    fun build(species: Pet.Species, @Suppress("UNUSED_PARAMETER") primaryColor: Int): SpannableStringBuilder {
        return SpannableStringBuilder(getTemplate(species).joinToString("\n"))
    }

    fun buildSmall(species: Pet.Species, @Suppress("UNUSED_PARAMETER") primaryColor: Int): SpannableStringBuilder {
        return SpannableStringBuilder(getSmallTemplate(species).joinToString("\n"))
    }

    private fun getTemplate(species: Pet.Species): List<String> = when (species) {
        Pet.Species.OCTOPUS -> listOf(
            "     ___       ",
            "    /   \\      ",
            "   | o o |     ",
            "   |  ~  |     ",
            "    \\___/      ",
            "   /|||||\\     ",
            "  / ||||| \\    ",
            " ~  ~~ ~~  ~   ",
        )
        Pet.Species.CAT -> listOf(
            "   /\\_/\\       ",
            "  ( o.o )      ",
            "   > ^ <       ",
            "  /|   |\\      ",
            " (_|   |_)     ",
        )
        Pet.Species.RABBIT -> listOf(
            "   (\\(\\        ",
            "   ( -.-)      ",
            "   o_(\")(\"')   ",
        )
        Pet.Species.FOX -> listOf(
            "   /\\_/\\       ",
            "  ( ^.^ )      ",
            "   \\ V /       ",
            "   /   \\       ",
            "  (_\\ /_)      ",
        )
        Pet.Species.BIRD -> listOf(
            "    __         ",
            "   (  )>       ",
            "   ||          ",
            "   ^^          ",
        )
        Pet.Species.ROBOT -> listOf(
            "   [===]       ",
            "   |o o|       ",
            "   |___|       ",
            "   /| |\\       ",
            "  d|   |b      ",
        )
        Pet.Species.DRAGON -> listOf(
            "   /\\_/\\       ",
            "  ( @ @ )      ",
            "  />-w-<\\      ",
            " /  |=|  \\     ",
            "~   d b   ~    ",
        )
        Pet.Species.PENGUIN -> listOf(
            "    .___.      ",
            "   / o o \\     ",
            "  |   >   |    ",
            "  |  \\_/  |    ",
            "   \\_____/     ",
            "    || ||       ",
        )
        Pet.Species.PANDA -> listOf(
            "   .-\"\"\"-.     ",
            "  /@ _ _ @\\    ",
            "  |  (_)  |    ",
            "  \\  ---  /    ",
            "   '-...-'     ",
        )
    }

    private fun getSmallTemplate(species: Pet.Species): List<String> = when (species) {
        Pet.Species.OCTOPUS -> listOf(
            "  ___  ",
            " (o.o) ",
            " /|||\\ ",
            " ~ ~ ~ ",
        )
        Pet.Species.CAT -> listOf(
            " /\\_/\\ ",
            "(o.o ) ",
            " > ^ < ",
        )
        Pet.Species.RABBIT -> listOf(
            " (\\(\\ ",
            " (-.-)  ",
            " (\")(\") ",
        )
        Pet.Species.FOX -> listOf(
            " /\\_/\\ ",
            "( ^.^ )",
            "  \\ / ",
        )
        Pet.Species.BIRD -> listOf(
            "  __  ",
            " (  )>",
            "  ^^ ",
        )
        Pet.Species.ROBOT -> listOf(
            " [==] ",
            " |oo| ",
            " |__| ",
        )
        else -> getSmallTemplate(Pet.Species.OCTOPUS)
    }
}
