"""
TAM OTOMATİK: konu ver -> Beyin (Gemini) senaryo yazar -> Üretici video yapar.
Tek komut:  python auto.py "Stuxnet: Iran nukleer santralini vuran ilk siber silah"

Gerekli: GEMINI_API_KEY (Beyin icin) + calisan servisler (start_all.bat).
Not: Gemini API'ye erisebilen bir makinede calisir.
"""
import sys, json
import brain
import produce

if __name__ == "__main__":
    topic = " ".join(sys.argv[1:])
    if not topic:
        print("Kullanim: python auto.py \"<konu>\"")
        sys.exit(1)

    print(f"=== BEYİN ({brain.BACKEND}) senaryo yazıyor: {topic} ===")
    scenario = brain.generate_scenario(topic)
    if scenario.get("rejected"):
        print("REDDEDİLDİ:", scenario.get("reason"), "| öneri:", scenario.get("alternative"))
        sys.exit(0)
    print("HOOK:", scenario.get("hook_overlay"))
    print("MOD :", scenario.get("visual_mode"))

    # senaryoyu da diske birak (kayit + istersen elle duzenle)
    with open("scenario.json", "w", encoding="utf-8") as f:
        json.dump(scenario, f, ensure_ascii=False, indent=2)

    print("=== ÜRETİCİ video yapıyor ===")
    produce.produce(scenario)
