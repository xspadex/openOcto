package com.openocto.app

import android.text.SpannableStringBuilder
import android.text.Spannable
import android.text.style.ForegroundColorSpan

/**
 * Generates colored pixel art for each pet species using unicode block characters.
 * Each species has a template with color tokens, rendered with the pet's color.
 */
object PetPixelArt {

    private const val F = '\u2588' // full block

    // Color tokens in templates:
    //   C = primary color, D = dark shade, L = light shade
    //   W = white, B = black, P = pink, T = transparent (space)

    fun build(species: Pet.Species, primaryColor: Int): SpannableStringBuilder {
        val rows = getTemplate(species)
        return render(rows, primaryColor)
    }

    fun buildSmall(species: Pet.Species, primaryColor: Int): SpannableStringBuilder {
        val rows = getSmallTemplate(species)
        return render(rows, primaryColor)
    }

    private fun getTemplate(species: Pet.Species): List<String> = when (species) {
        Pet.Species.OCTOPUS -> listOf(
            "   CCCCCCCC   ",
            "  CCCCCCCCCC  ",
            " CCWWCCCCWWCC ",
            " CCBWCCCCBWCC ",
            " CLLCCPPCCLLC ",
            "  CCCCCCCCCC  ",
            "  DC DC DC DC ",
            "  DC DC DC DC ",
        )
        Pet.Species.CAT -> listOf(
            "  DC          CD    ",
            "  DCC        CCD    ",
            "  DCCCCCCCCCCCC D   ",
            "  CCCCCCCCCCCCCC    ",
            "  CCWWBCCCCWWBCC    ",
            "  CCWWBCCCCWWBCC    ",
            "  CCCCCCDDCCCCCC    ",
            "  CCCCCCCCCCCCCC    ",
            "   CCCCCCCCCCCC     ",
            "    CCCCCCCCCC      ",
            "     CCCCCCCC       ",
            "      CC  CC        ",
        )
        Pet.Species.RABBIT -> listOf(
            "    CC    CC        ",
            "   CCCC  CCCC       ",
            "   CCCC  CCCC       ",
            "   CCCCCCCCCC       ",
            "  CCCCCCCCCCCC      ",
            "  CCWWBCCWWBCC      ",
            "  CCCCCCCCCCCC      ",
            "  PPCCCCCCCCPP      ",
            "  CCCCDDDDCCCC      ",
            "   CCCCCCCCCC       ",
            "    CCCCCCCC        ",
            "     CC  CC         ",
        )
        Pet.Species.FOX -> listOf(
            "  DD          DD    ",
            "  DCCC      CCCD    ",
            "  DCCCCCCCCCCCC D   ",
            "  CCCCCCCCCCCCCC    ",
            "  CCWWBCCCCWWBCC    ",
            "  CCWWBCCCCWWBCC    ",
            "  LLCCCCDDCCCCLL    ",
            "  CCCCLLLLLLCCCC    ",
            "   CCCCCCCCCCCC     ",
            "    CCCCCCCCCC      ",
            "     DDCCCCDD       ",
            "      DD  DD        ",
        )
        Pet.Species.BIRD -> listOf(
            "       CCCC         ",
            "     CCCCCCCC       ",
            "    CCCCCCCCCC      ",
            "   CCCCCCCCCCCC     ",
            "   CCWWBCCWWBCC     ",
            "   CCCCCCCCCCCC     ",
            "  DDCCCCCCCCCCDD    ",
            "   CCCCDDDDCCCC     ",
            "    CCCCCCCCCC      ",
            "     CCCCCCCC       ",
            "      CC  CC        ",
            "     CC    CC       ",
        )
        Pet.Species.ROBOT -> listOf(
            "     DDDDDDDD       ",
            "    CCCCCCCCCC      ",
            "   CCCCCCCCCCCC     ",
            "   CCLLBCCLLBCC     ",
            "   CCLLBCCLLBCC     ",
            "   CCCCCCCCCCCC     ",
            "   CCDDDDDDDDC C   ",
            "   CCCCCCCCCCCC     ",
            "    CCCCCCCCCC      ",
            "   DDCCCCCCCCDD     ",
            "   DD CC  CC DD     ",
            "      DD  DD        ",
        )
        Pet.Species.DRAGON -> listOf(
            "  DCC        CCD    ",
            "   DCCCCCCCCCCD     ",
            "  CCCCCCCCCCCCCC    ",
            "  CCWWBCCCCWWBCC    ",
            "  CCCCCCCCCCCCCC    ",
            "  CCCCDDDDDDCCCC    ",
            "   CCCCCCCCCCCC     ",
            " DD CCCCCCCCCC DD   ",
            "     CCCCCCCC       ",
            "    DDCCCCCCDD      ",
            "      CC  CC        ",
            "     DDD  DDD       ",
        )
        Pet.Species.PENGUIN -> listOf(
            "      BBBBBB        ",
            "    BBBBBBBBBB      ",
            "   BBWWBBBBWWBB     ",
            "   BBBBBBBBBBBB     ",
            "   BBBWWBBWWBBB     ",
            "   BBBWWBBWWBBB     ",
            "   BBWWWWWWWWBB     ",
            "   BBWWWDDWWWBB     ",
            "    BBWWWWWWBB      ",
            "     BBBBBBBB       ",
            "      CC  CC        ",
            "     CCC  CCC       ",
        )
        Pet.Species.PANDA -> listOf(
            "    CCCCCCCCCC      ",
            "   CCCCCCCCCCCC     ",
            "  BBCCCCCCCCCCBB    ",
            "  BBWWBCCCCWWBBB    ",
            "  BBWWBCCCCWWBBB    ",
            "   CCCCCCCCCCCC     ",
            "   CCCCBBBBCCCC     ",
            "    CCCCCCCCCC      ",
            "   BBCCCCCCCCBB     ",
            "   BBCCCCCCCCBB     ",
            "     CC    CC       ",
            "     BB    BB       ",
        )
    }

    private fun getSmallTemplate(species: Pet.Species): List<String> = when (species) {
        Pet.Species.OCTOPUS -> listOf(
            "  CCCCCC  ",
            " CCCCCCCC ",
            " CWBCCWBC ",
            " CCCCCCCC ",
            "  CCCCCC  ",
            " DC DC DC ",
        )
        Pet.Species.CAT -> listOf(
            " DC    CD ",
            " CCCCCCCC ",
            " CWBCCWBC ",
            " CCCCCCCC ",
            "  CCCCCC  ",
            "   CC CC  ",
        )
        Pet.Species.RABBIT -> listOf(
            "  CC  CC  ",
            " CCCCCCCC ",
            " CWBCCWBC ",
            " CCCCCCCC ",
            "  CCCCCC  ",
            "   CC CC  ",
        )
        Pet.Species.FOX -> listOf(
            " DC    CD ",
            " CCCCCCCC ",
            " CWBCCWBC ",
            " LLCCCCLL ",
            "  CCCCCC  ",
            "   DD DD  ",
        )
        Pet.Species.BIRD -> listOf(
            "   CCCC   ",
            "  CCCCCC  ",
            " CWBCCWBC ",
            " DCCCCCC D",
            "  CCCCCC  ",
            "  CC  CC  ",
        )
        Pet.Species.ROBOT -> listOf(
            "  DDDDDD  ",
            " CCCCCCCC ",
            " CLBCCLBC ",
            " CDDDDDC  ",
            "  CCCCCC  ",
            "  DD  DD  ",
        )
        else -> getSmallTemplate(Pet.Species.OCTOPUS) // fallback
    }

    private fun render(rows: List<String>, primaryColor: Int): SpannableStringBuilder {
        val C = primaryColor
        val D = darken(primaryColor, 0.7f)
        val L = lighten(primaryColor, 0.4f)
        val W = 0xFFFFFFFF.toInt()
        val B = 0xFF1A1A2E.toInt()
        val P = 0xFFFF80AB.toInt()

        val sb = SpannableStringBuilder()
        for ((ri, row) in rows.withIndex()) {
            for (ch in row) {
                val color = when (ch) {
                    'C' -> C; 'D' -> D; 'L' -> L
                    'W' -> W; 'B' -> B; 'P' -> P
                    else -> 0
                }
                if (color == 0) {
                    sb.append(" ")
                } else {
                    val start = sb.length
                    sb.append(F.toString())
                    sb.setSpan(
                        ForegroundColorSpan(color),
                        start, sb.length,
                        Spannable.SPAN_EXCLUSIVE_EXCLUSIVE
                    )
                }
            }
            if (ri < rows.lastIndex) sb.append("\n")
        }
        return sb
    }

    private fun darken(color: Int, factor: Float): Int {
        val r = ((color shr 16) and 0xFF) * factor
        val g = ((color shr 8) and 0xFF) * factor
        val b = (color and 0xFF) * factor
        return (0xFF shl 24) or (r.toInt() shl 16) or (g.toInt() shl 8) or b.toInt()
    }

    private fun lighten(color: Int, factor: Float): Int {
        val r = ((color shr 16) and 0xFF) + ((255 - ((color shr 16) and 0xFF)) * factor)
        val g = ((color shr 8) and 0xFF) + ((255 - ((color shr 8) and 0xFF)) * factor)
        val b = (color and 0xFF) + ((255 - (color and 0xFF)) * factor)
        return (0xFF shl 24) or (r.toInt().coerceIn(0, 255) shl 16) or
                (g.toInt().coerceIn(0, 255) shl 8) or b.toInt().coerceIn(0, 255)
    }
}
