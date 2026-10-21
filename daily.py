"""
GÜNLÜK ÇALIŞMA: her kanal icin videos_per_day kadar video uret + yukle.
Windows Task Scheduler daily.bat'i cagirir; o da bunu calistirir.
Servisler (start_all.bat) acik olmali.
"""
import datetime
import sys

import run
import saglik

if __name__ == "__main__":
    print(f"\n########## {datetime.datetime.now():%Y-%m-%d %H:%M} GÜNLÜK ÇALIŞMA ##########")
    # ÖNCE SAĞLIK: 2026-10-15'te sistem haftalarca sessizce bozuktu. Zamanlanmış
    # çalışmada kimse bakmadığı için, bozuk katmanla üretime girmek yerine burada
    # durup sebebi yazıyoruz (çıktı daily.bat log'una düşer).
    if saglik.main() != 0:
        print("!!! SAĞLIK KONTROLÜ BAŞARISIZ -> günlük üretim İPTAL edildi.")
        sys.exit(1)
    chans = run.load_channels()
    for ch, prof in chans.items():
        n = int(prof.get("videos_per_day", 1))
        print(f"\n===== {ch}: {n} video =====")
        for i in range(n):
            try:
                run.run_one(ch)          # kuyruktan/üret -> video -> yükle
            except Exception as e:
                print(f"  HATA ({ch} #{i+1}): {e}")
    print(f"########## BİTTİ {datetime.datetime.now():%H:%M} ##########\n")
