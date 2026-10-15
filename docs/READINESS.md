# Hazırlık durumu

**Hüküm: PAPER TESTING READY — gerçek para için değil.** Solunaro bir ölçüm düzeneğidir; gerçek işlem yürütme katmanı yoktur ve eklenmemelidir.

## Doğrulanmış olanlar (çevrimdışı testlerle)

- Kodla zorlanan risk kuralları ve fail-closed girdi doğrulama.
- Son kapı: NOVA + TITAN + kod kontrolleri; hiçbir LLM tek başına işlem açamaz.
- Paper giriş/çıkış mantığı backtest ile aynı tanımları kullanır.
- Yeniden başlatmada limitlerin ve açık pozisyonların korunması; çift kapanış yok.
- Kalibrasyon: rastgele ajanlarla masa-baseline testi 0/40, seçim testi 1/40 yanlış alarm.

## Henüz kanıtı olmayanlar

- **Gerçek performans ölçümü yok.** Henüz kapanmış paper işlem yok; masanın veya baseline'ın kârlı olduğuna dair bir veri yok.
- Baseline geçmiş testi (`run.py backtest`) bu sürümde çalıştırılmadı (denetim ortamında ağ erişimi yoktu). Sunucuda çalıştırılmalı.
- Canlı Anthropic çağrısı hiç yapılmadı.
- Ajan değeri testi hedefinden (≈%1) biraz sık yanlış alarm veriyor (%2, 40 tohum). Tek rapora dayanarak ajan kapatılmamalı.

## Bilinen açıklar

- Giriş fiyatı son kapanmış mumun kapanışıdır (karar anındaki fiyat değil); 90 dakikaya kadar gecikmeli dolum iyimserlik yaratabilir.
- RSS XML'i standart kütüphane ile ayrıştırılıyor (bandit B314). Kaynaklar sabit ve bilinen sitelerdir; `defusedxml` henüz eklenmedi.
- Haber metinleri LLM'e gidiyor; prompt injection en fazla ORION'un görüşünü etkileyebilir, kod kontrollerini aşamaz.
- Telegram mesajları hız sınırına tabi değil (hata uyarıları saatte bir ile sınırlı).
- Desk-vs-baseline testi kümesiz bootstrap kullanıyor; pozisyonlar tek tek açıldığı için bağımlılık düşük, yine de yorumlarken dikkat.

## Sonuçlar nasıl okunur?

- `INSUFFICIENT SAMPLE`: karar yok, beklemeye devam.
- `NO SIGNIFICANT DIFFERENCE`: LLM'ler baseline'dan iyi değil (henüz).
- `Desk ahead`: güven aralığı sıfırı dışlıyor. Tek bir rapor yeterli değildir; haftalar boyunca tutarlı kalmalı. Paper kazancı gerçek kazanç garantisi değildir.
