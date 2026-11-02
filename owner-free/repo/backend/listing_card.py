"""Presentation shared by AUTO.RIA deliveries after source-specific validation."""
from decimal import Decimal


def market_lines(price, market):
    price, market = Decimal(str(price)), Decimal(str(market))
    amount = f"{market:,.2f}".rstrip("0").rstrip(".").replace(",", " ").replace(".", ",")
    difference = (market - price) / market * 100
    percent = f"{abs(difference):.1f}".rstrip("0").rstrip(".").replace(".", ",")
    label = "🔥 Вигода" if difference >= 0 else "📈 Вище ринкової ціни"
    return [f"📊 Ринкова ціна: ≈ ${amount}", f"{label}: {percent}%"]


def listing_caption(brand, model, year, fuel, transmission, mileage_km, region,
                    price, pricing, *, active_window=False):
    sections = [f"🚘 {brand} {model} · {year}"]
    if active_window:
        sections.append("🕘 Активне оголошення з додаткової перевірки")
    details = []
    if fuel:
        details.append(f"⛽️ {fuel}")
    if transmission:
        details.append(f"⚙️ {transmission}")
    if mileage_km is not None:
        mileage = f"{float(mileage_km) / 1000:g}"
        details.append(f"🛣️ Пробіг: {mileage.replace('.', ',')} тис. км")
    if region:
        details.append(f"📍 {region}")
    if details:
        sections.append("\n".join(details))
    amount = f"{Decimal(str(price)):,.0f}".replace(",", " ")
    sections.append("\n".join([f"💰 Ціна: ${amount}", *pricing]))
    sections.append("/stop — вимкнути сповіщення")
    return "\n\n".join(sections)
