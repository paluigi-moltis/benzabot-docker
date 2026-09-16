"""Italian-language message formatting for the Telegram bot."""

from datetime import datetime

FUEL_ORDER = [
    "Benzina",
    "Benzina plus",
    "Gasolio",
    "Gasolio plus",
    "GPL",
    "Metano",
    "L-GNC",
    "GNL",
    "GNC",
    " idrogeno",
]

MONTHS_IT = [
    "gennaio", "febbraio", "marzo", "aprile", "maggio", "giugno",
    "luglio", "agosto", "settembre", "ottobre", "novembre", "dicembre",
]


def _fmt_dt(dt):
    if not isinstance(dt, datetime):
        return str(dt or "n/d")
    return f"{dt.day} {MONTHS_IT[dt.month - 1]} {dt.year}, {dt:%H:%M}"


def format_station_message(station, max_prezzi=25):
    """Format one station + full price list as an HTML message."""
    lines = []
    header = f"<b>{station.get('bandiera') or station.get('gestore') or 'Distributore'}</b>"
    indirizzo = ", ".join(
        part for part in (station.get("indirizzo"), station.get("comune"), station.get("provincia")) if part
    )
    if indirizzo:
        header += f"\n📍 {indirizzo}"
    dist = station.get("distance_m")
    if dist is not None:
        header += f"\n📏 {dist / 1000:.1f} km da te"
    lines.append(header)

    prezzi = station.get("prezzi") or []
    prezzi = sorted(prezzi, key=lambda p: (FUEL_ORDER.index(p["carburante"]) if p["carburante"] in FUEL_ORDER else len(FUEL_ORDER), p["carburante"], not p.get("is_self", False)))
    if prezzi:
        lines.append("")
        for p in prezzi[:max_prezzi]:
            tipo = "self" if p.get("is_self") else "servito"
            lines.append(f"• <b>{p['carburante']}</b> ({tipo}): {p['prezzo']:.3f} €/L")
        if len(prezzi) > max_prezzi:
            lines.append(f"… e altri {len(prezzi) - max_prezzi} carburanti")
        dts = [p.get("dt_comu") for p in prezzi if isinstance(p.get("dt_comu"), datetime)]
        if dts:
            lines.append(f"\n🗓 Prezzi comunicati il {_fmt_dt(max(dts))}")
    else:
        lines.append("\n⚠️ Nessun prezzo disponibile per questo impianto.")
    return "\n".join(lines)


def navigation_keyboard(station):
    """Inline keyboard buttons opening the station in navigation apps."""
    from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton

    lat, lon = station["lat"], station["lon"]
    gmaps = f"https://www.google.com/maps/dir/?api=1&destination={lat},{lon}&travelmode=driving"
    osm = f"https://www.openstreetmap.org/?mlat={lat}&mlon={lon}#map=17/{lat}/{lon}"
    apple = f"https://maps.apple.com/?daddr={lat},{lon}&dirflg=d"
    kb = InlineKeyboardMarkup(row_width=2)
    kb.row(
        InlineKeyboardButton("🅖 Google Maps", url=gmaps),
        InlineKeyboardButton("🍎 Apple Maps", url=apple),
    )
    kb.add(InlineKeyboardButton("🗺 OSM (tutte le app)", url=osm))
    return kb


def format_info_message(status, repo_url):
    base = (
        "🛢 <b>Benzabot</b> — prezzi dei carburanti intorno a te.\n\n"
        "Inviami la tua <b>posizione GPS</b> (icone 📎 → Posizione → Posizione attuale) "
        "e ti risponderò con le 3 stazioni di rifornimento più vicine, "
        "ordinate per distanza, con l'elenco completo dei prezzi di ogni carburante.\n\n"
        "Da ogni messaggio puoi aprire la rotta verso il distributore con la tua app di navigazione.\n\n"
        "Comandi:\n"
        "/info — questa guida\n"
        "/github — codice sorgente del progetto\n\n"
        "I dati provengono dagli open data del MIMIT "
        "(prezzi comunicati dai gestori, vigili alle 8 del giorno precedente)."
    )
    if status:
        estrazione = status.get("extraction_date")
        estrazione_str = estrazione.strftime("%d/%m/%Y") if hasattr(estrazione, "strftime") else (estrazione or "n/d")
        n_stazioni = status.get("n_stazioni", "n/d")
        n_prezzi = status.get("n_stazioni_con_prezzi", "n/d")
        updated = status.get("completed_at")
        updated_str = updated.strftime("%d/%m/%Y %H:%M UTC") if hasattr(updated, "strftime") else "n/d"
        ok = "✅" if status.get("ok") else "⚠️ ultimo aggiornamento fallito"
        base += (
            f"\n\nStato dati: {ok}\n"
            f"• Estrazione MIMIT: {estrazione_str}\n"
            f"• Impianti con coordinate valide: {n_stazioni}\n"
            f"• Impianti con prezzi: {n_prezzi}\n"
            f"• Aggiornato il: {updated_str}"
        )
    return base
