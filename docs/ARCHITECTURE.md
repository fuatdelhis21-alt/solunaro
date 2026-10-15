# Mimari

Solunaro **yalnızca paper** çalışır. Projede borsa anahtarı, cüzdan, imza ya da emir gönderen kod yoktur. Tek dış yazma işlemi isteğe bağlı Telegram mesajıdır.

## Modüller

| Dosya | Görev |
|---|---|
| `run.py` | Komut satırı: `once`, `loop`, `status`, `data`, `eval`, `dashboard`, `backtest`, `keycheck`. `once`/`loop` tek süreç kilidi alır. |
| `desk/data.py` | Binance, GeckoTerminal, DexScreener, RSS, Fear & Greed, futures, CryptoPanic. Mum doğrulama (`clean_candles`), sınırlı HTTP tekrarı (`_get`). |
| `desk/indicators.py` | EMA, RSI, ATR, swing seviyeleri, `summarize()`. LLM yok. |
| `desk/baseline.py` | LLM'siz kural: ön filtre ve baseline hesabının sinyali. |
| `desk/risk.py` | Kodla zorlanan kurallar: girdi doğrulama, grounding, mesafe, maliyet sonrası R:R, boyut, limitler. |
| `desk/agents.py` | Altı ajanın prompt'ları ve çıktı doğrulama (`normalize`). |
| `desk/llm.py` | Anthropic Messages API istemcisi (HTTP). Ret/`max_tokens` durumunda hata, token ve gecikme kaydı. |
| `desk/engine.py` | Döngü: izleme, veri toplama, baseline adımı, LLM döngüsü, son kapı, shadow kaydı. |
| `desk/paper.py` | SQLite paper defteri: pozisyonlar, kararlar, shadow'lar, LLM kullanımı, meta. |
| `desk/evaluate.py` | Masa vs baseline, seçim testi, ajan değeri, harcama, operasyon metrikleri. |
| `desk/backtest.py` | Baseline kuralının geçmiş testi (LLM masası geçmişte test edilmez). |
| `desk/dashboard.py`, `desk/notify.py` | HTML rapor, tek yönlü Telegram. |

## Bir karar döngüsü (`Engine.decide`)

1. Kill switch (`STOP` dosyası) varsa hiçbir şey yapılmaz.
2. **Veri toplama:** Her piyasa için mumlar çekilir ve doğrulanır. 30'dan az kapanmış mum, 90 dakikadan eski veri, DEX'te likidite < 250 bin $ veya 24s hacim < 500 bin $ (DexScreener'ın USD alanları) → piyasa uygun değil.
3. **Baseline adımı:** LLM'siz kural, kendi ayrı paper defterinde aynı risk kurallarıyla işlem açar.
4. Masada açık pozisyon varsa LLM çağrılmaz.
5. **Ön filtre:** Baseline sinyali olmayan piyasalar LLM'e gitmez. Uygun piyasa yoksa döngü `NO_MARKET` ile biter (ücret yok).
6. **ATLAS** uygun piyasalardan birini seçer. Listede olmayan seçim → `NO_PICK`.
7. **VEGA** (grafik), **ORION** (haber), **LUNA** (duygu/pozisyonlanma) paralel çalışır.
8. **Kod risk kontrolleri** (`risk.evaluate`) + piyasa türüne göre haber şartı.
9. **TITAN** risk yöneticisi, **NOVA** son karar.
10. **Son kapı:** `NOVA == TRADE` **ve** `TITAN == APPROVE` **ve** tüm kod kontrolleri PASS. Aksi halde karar `REJECT` olarak yazılır ve nedeni `failed_checks=` ile kaydedilir.
11. **Shadow:** Kurulumu geçerli (grounded seviyeler, mesafeler, maliyet sonrası R:R) her setup, alınsın alınmasın kaydedilir.
12. Rapor `data/reports/` altına JSON olarak yazılır.

`Engine.monitor` her 5 dakikada açık pozisyonları ve shadow'ları kapanmış mumlarla kontrol eder.

## Veritabanı (SQLite, `data/desk.sqlite` ve `data/baseline.sqlite`)

- `positions`: `status` OPEN → CLOSED. `entry_ts` = giriş fiyatını veren mumun kapanış zamanı.
- `decisions`: her döngünün sonucu (`nova` alanı: TRADE/WAIT/REJECT/SKIP/ERROR/NO_MARKET/NO_PICK), `experiment` kimliği.
- `shadows`: karşı-olgusal setup'lar, `status` OPEN → TARGET/STOP/EXPIRED.
- `llm_usage`: ajan, *cevap veren* model, token, gecikme.
- `meta`: başlangıç bakiyesi, hata sayaçları, son başarılı veri zamanı.

Şema değişiklikleri yalnızca ekleme yapar (`ALTER TABLE ... ADD COLUMN`); mevcut satırlar korunur.

## Durum geçişleri

Masada ayrı bir `PENDING` durumu yoktur: paper pozisyon, son kapı geçildiği anda `OPEN` olarak yazılır. Reddedilen kararlar `positions` tablosuna hiç girmez; `decisions` tablosunda `REJECT`/`SKIP` olarak durur. `CLOSED` geçişi yalnızca `status='OPEN'` satırda yapılır, bu yüzden bir pozisyon iki kez kapanamaz.
