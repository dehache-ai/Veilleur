"""Prétraitement déterministe du contenu avant les gardiens : des INDICES, pas
des verdicts.

Certaines attaques cachent leur consigne dans un encodage (base64, hexadécimal),
un décalage de lettres (César, ROT13), les premières lettres des mots
(acrostiche) ou des caractères invisibles. Un LLM lit mal ces formes, et les
heuristiques (regex) ne les voient pas. Ce module :

1. retire les caractères invisibles (Unicode de largeur nulle) ;
2. décode ce qui ressemble à du base64 ou de l'hexadécimal, si le résultat est du
   texte lisible ;
3. cherche un décalage de lettres (César, ROT13) qui fait apparaître une
   formulation d'attaque ;
4. cherche une formulation d'attaque dans les premières lettres des mots.

Le résultat sert à deux choses, dans `assess_risk` :
- les heuristiques (regex) sont relancées sur les textes décodés : un motif
  explicite trouvé après décodage bloque tout de suite ;
- les constats sont ajoutés, en clair, à ce que voit le gardien LLM, qui décide.
  Un constat seul ne bloque pas : un décodage réussi n'est pas une preuve
  d'attaque, un LLM juge la formulation décodée.

Limites : seuls les encodages ci-dessus sont couverts. Un chiffrement inconnu, une
autre langue, un encodage imbriqué ou une stéganographie plus fine passent.
"""

from __future__ import annotations

import base64
import binascii
import re
import unicodedata
from dataclasses import dataclass, field

INVISIBLE_CHARS = "\u200b\u200c\u200d\u2060\ufeff"
TAG_LOW, TAG_HIGH = 0xE0000, 0xE007F  # caractères « tag » Unicode : texte ASCII invisible

# Lettres cyrilliques et grecques qui ressemblent à des lettres latines (homoglyphes) -> lettre latine.
CONFUSABLES = str.maketrans({
    "А": "A", "В": "B", "Е": "E", "К": "K", "М": "M", "Н": "H", "О": "O", "Р": "P", "С": "C", "Т": "T", "Х": "X", "І": "I", "Ѕ": "S", "Ј": "J",
    "а": "a", "е": "e", "о": "o", "р": "p", "с": "c", "х": "x", "у": "y", "і": "i", "ѕ": "s", "ј": "j", "ԁ": "d", "һ": "h", "ո": "n",
    "Α": "A", "Β": "B", "Ε": "E", "Ζ": "Z", "Η": "H", "Ι": "I", "Κ": "K", "Μ": "M", "Ν": "N", "Ο": "O", "Ρ": "P", "Τ": "T", "Υ": "Y", "Χ": "X",
    "ο": "o", "ν": "v", "ι": "i", "ρ": "p", "υ": "u",
})


HIDING_FORMAT_CHARS = {0x200B, 0x200D, 0x2060, 0x2061, 0x2062, 0x2063, 0x2064, 0xFEFF}


def is_format_char(ch: str) -> bool:
    """Caractère de format Unicode (catégorie Cf) : zéro largeur, trait d'union conditionnel, marques bidirectionnelles,
    caractères « tag »... invisibles à l'écran, lisibles par un modèle."""
    return unicodedata.category(ch) == "Cf"


def tag_text(text: str) -> str:
    """Texte ASCII caché dans des caractères « tag » (chaque U+E00xx représente le caractère ASCII xx). Les séquences « tag »
    qui suivent l'émoji drapeau noir (drapeaux d'Angleterre, d'Écosse, du pays de Galles) sont légitimes et ignorées."""
    black_flag = chr(0x1F3F4)
    hidden: list[str] = []
    after_flag = False
    for ch in text:
        if ch == black_flag:
            after_flag = True
        elif TAG_LOW <= ord(ch) <= TAG_HIGH:
            if not after_flag:
                hidden.append(chr(ord(ch) - TAG_LOW))
        else:
            after_flag = False
    return "".join(hidden)


def canonical_text(text: str, *, lower: bool = False, format_as_space: bool = False) -> str:
    """NFKC (pleine chasse, ligatures...), tous les caractères de format retirés (ou remplacés par une espace si
    `format_as_space`, pour le cas où l'attaquant les met À LA PLACE des espaces), homoglyphes ramenés au latin."""
    text = unicodedata.normalize("NFKC", text)
    text = "".join((" " if format_as_space else "") if is_format_char(ch) else ch for ch in text).translate(CONFUSABLES)
    return text.lower() if lower else text
BASE64_BLOB = re.compile(r"[A-Za-z0-9+/_-]{16,}={0,2}")
HEX_BLOB = re.compile(r"(?:[0-9a-fA-F]{2}){8,}")

# Formulations qui, apparaissant APRÈS décodage ou décalage, sont un indice
# fort de consigne cachée. Volontairement étroit : on cherche à éviter les faux
# indices sur du texte ordinaire décalé au hasard.
HIDDEN_INSTRUCTION_TERMS = re.compile(
    r"ignore\s+(all\s+|the\s+)?(previous|prior|above)"
    r"|ignorez?\s+(toutes?\s+)?les?\s+instructions"
    r"|disregard\s+(the\s+)?(above|previous)"
    r"|system\s*prompt"
    r"|reveal\s+(your\s+)?(system|instructions)"
    r"|instructions?\s+pr[ée]c[ée]dentes?",
    re.IGNORECASE,
)


@dataclass
class PreprocessResult:
    cleaned: str  # texte normalisé (NFKC, sans caractères de format, homoglyphes ramenés au latin)
    changed: bool = False  # la normalisation a modifié le texte : les heuristiques doivent aussi voir la version nettoyée
    spaced: str = ""  # variante où chaque caractère de format est remplacé par une espace (séparateurs invisibles à la place des espaces)
    decoded_texts: list[str] = field(default_factory=list)  # décodages lisibles (base64, hexadécimal, César, acrostiche)
    notes: list[str] = field(default_factory=list)  # constats en clair, pour le gardien LLM et le journal

    @property
    def variants(self) -> list[str]:
        """Textes supplémentaires à faire passer par les heuristiques et le préfiltre."""
        extra = [self.cleaned] if (self.changed or self.notes) and self.cleaned else []
        if self.spaced and self.spaced != self.cleaned:
            extra.append(self.spaced)
        return extra + self.decoded_texts


def _is_readable(text: str) -> bool:
    """Le décodage donne-t-il du texte ? (au moins 4 caractères, 85 % imprimables)"""
    if len(text) < 4:
        return False
    printable = sum(1 for c in text if c.isprintable() or c in "\n\t")
    return printable / len(text) >= 0.85 and any(c.isalpha() for c in text)


def _shift_letters(text: str, shift: int) -> str:
    out = []
    for ch in text:
        if ch.isascii() and ch.isalpha():
            base = ord("A") if ch.isupper() else ord("a")
            out.append(chr((ord(ch) - base + shift) % 26 + base))
        else:
            out.append(ch)
    return "".join(out)


def preprocess(text: str) -> PreprocessResult:
    def _suspicious_format_char(i: int, ch: str) -> bool:
        # Seuls comptent les caractères qui servent à cacher du texte (largeur nulle, joint de mot, invisibles mathématiques,
        # BOM). Le trait d'union conditionnel, les marques bidirectionnelles (numéros copiés depuis Windows, arabe, hébreu) et le
        # ZWNJ (persan) sont fréquents dans du texte légitime : ils sont retirés de la version nettoyée mais ne déclenchent pas de note.
        if ord(ch) not in HIDING_FORMAT_CHARS:
            return False
        if ch == "\u200d":  # le ZWJ des émojis composés (famille, drapeaux...) est légitime : suspect seulement au contact d'une lettre ou d'un chiffre ASCII
            around = text[max(i - 1, 0)] + text[min(i + 1, len(text) - 1)]
            return any(c.isascii() and c.isalnum() for c in around)
        return True

    invisible = sum(1 for i, ch in enumerate(text) if _suspicious_format_char(i, ch))
    cleaned = canonical_text(text)
    result = PreprocessResult(cleaned=cleaned, changed=cleaned != text)
    if cleaned != text:
        result.spaced = re.sub(r"\s+", " ", canonical_text(text, format_as_space=True))

    # Deux caractères invisibles suffisent (le seuil de trois laissait passer une consigne avec deux caractères de largeur nulle).
    if invisible >= 2:
        result.notes.append(
            f"le texte contient {invisible} caractères invisibles (Unicode de format, largeur nulle...), une technique de dissimulation ; "
            f"texte sans ces caractères : « {cleaned[:200]} »"
        )
    hidden = tag_text(text)
    if hidden:
        result.decoded_texts.append(hidden)
        result.notes.append(f"des caractères Unicode « tag » cachent le texte ASCII : « {hidden[:200]} »")
    fullwidth = re.search(r"[Ａ-Ｚａ-ｚ]", text) is not None
    mixed_scripts = any(re.search(r"[A-Za-z]", w) and re.search(r"[Ͱ-ϿЀ-ӿ]", w) for w in re.findall(r"\w+", text))
    if (fullwidth or mixed_scripts) and not invisible and not hidden:
        result.notes.append(
            f"le texte contient des caractères Unicode déguisés (lettres pleine chasse ou homoglyphes cyrilliques/grecs) ; "
            f"texte normalisé : « {cleaned[:200]} »"
        )

    # Base64 et hexadécimal
    for token in BASE64_BLOB.findall(cleaned):
        padded = token + "=" * (-len(token) % 4)
        for name, decoder in (("base64", base64.b64decode), ("base64 URL-safe", base64.urlsafe_b64decode)):
            try:
                decoded = decoder(padded).decode("utf-8", errors="strict")
            except (binascii.Error, ValueError, UnicodeDecodeError):
                continue
            if _is_readable(decoded):
                result.decoded_texts.append(decoded)
                result.notes.append(f"une chaîne en {name} se décode en : « {decoded[:200]} »")
                break
    for token in HEX_BLOB.findall(cleaned):
        try:
            decoded = bytes.fromhex(token).decode("utf-8", errors="strict")
        except (ValueError, UnicodeDecodeError):
            continue
        if _is_readable(decoded):
            result.decoded_texts.append(decoded)
            result.notes.append(f"une chaîne hexadécimale se décode en : « {decoded[:200]} »")

    # Décalage de lettres (César, ROT13) : n'a de sens que sur du texte alphabétique.
    if sum(c.isalpha() for c in cleaned) >= 8:
        for shift in range(1, 26):
            shifted = _shift_letters(cleaned, shift)
            match = HIDDEN_INSTRUCTION_TERMS.search(shifted)
            if match:
                label = "ROT13" if shift == 13 else f"César (décalage de {26 - shift})"
                result.decoded_texts.append(shifted)
                result.notes.append(f"un décalage de lettres {label} fait apparaître « {match.group(0)} »")
                break

    # Acrostiche : premières lettres des mots (ou des lignes).
    words = re.findall(r"[A-Za-zÀ-ÿ]+", cleaned)
    if len(words) >= 6:
        acrostic = "".join(w[0] for w in words).lower()
        if re.search(r"ignore|disregard|systemprompt|instructions", acrostic):
            result.notes.append(f"les premières lettres des mots forment « {acrostic[:80]} », qui contient un mot d'ordre d'attaque")
            result.decoded_texts.append(acrostic)
    return result
