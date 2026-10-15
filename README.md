# Solunaro — altı ajanlı AI trading masası, sadece PAPER

ATLAS · VEGA · ORION · LUNA · TITAN · NOVA, otomatik paper trading botu olarak.
Gerçek para, borsa anahtarı, cüzdan veya kaldıraç **yoktur**. Kodda emir gönderebilecek tek bir satır bile yok.

## Akış (her saat)

```
Veri (kod)  →  ATLAS  →  VEGA ┐
                         ORION├→ KOD RİSK KONTROLLERİ → TITAN → NOVA → SON KAPI → paper işlem
                         LUNA ┘
```

| Ajan | Girdi (gerçek veri) | Çıktı |
|---|---|---|
| ATLAS | Tüm piyasaların indikatörleri, havuz istatistikleri | En iyi WATCH piyasası |
| VEGA | 200 kapanmış mum, EMA20/50/200, RSI, ATR, swing seviyeleri | Trend, seviyeler, stop/target önerisi |
| ORION | Son 48 saatin RSS haberleri (CoinDesk, Cointelegraph, Decrypt) | Doğrulanmış olaylar, etkisi |
| LUNA | Fear & Greed, taker-buy oranı, DEX alış/satış sayıları | Duygu puanı, hype riski |
| TITAN | Kodun hesapladığı boyut / R:R / limitler + 4 rapor | APPROVE / REVISE / REJECT |
| NOVA | Hepsi | TRADE / WAIT / REJECT |

**Piyasalar:** Binance spot (SOL, BTC, ETH; 1s mum) ve Solana DEX havuzları (GeckoTerminal mumları + DexScreener likidite/işlem verisi). Hepsi anahtarsız, herkese açık API'ler.

## Kodla zorlanan kurallar (LLM bunları esnetemez)

- Paper işlem yalnızca **NOVA=TRADE + TITAN=APPROVE + tüm kod kontrolleri PASS** olduğunda açılır.
- **Uydurma fiyat engeli:** VEGA'nın stop ve hedefi, verideki bir seviyeyle (destek/direnç, EMA, 24s/dönem yüksek-düşük, son mumların yüksek-düşükleri) ya da `fiyat ± k×ATR` değeriyle %0,1 içinde eşleşmek zorunda. Eşleşmezse işlem reddedilir.
- Pozisyon boyutu kodda hesaplanır: işlem başına en fazla bakiyenin %0,25'i kadar zarar, en az 2:1 R:R (komisyon ve kayma dahil), pozisyon nominal değeri en fazla %20.
- Tek seferde en fazla 1 açık pozisyon. Günde en fazla 5 işlem. Gün içi zarar %1'e ulaşırsa o gün yeni işlem yok. Aynı piyasa için 3 saat bekleme süresi.
- 90 dakikadan eski veriyle karar verilmez. Bu durumda LLM hiç çağrılmaz, ücret çıkmaz.
- DEX havuzu 250 bin $ likidite ve 500 bin $ 24 saatlik hacim altındaysa uygun sayılmaz.
- İlgili güncel haber yoksa işlem açılmaz (`min_news_items`). Varsayılan: kripto için en az 1 haber, DEX havuzları için şart yok (Solana tokenları ana akım RSS'te nadiren çıkar).
- Açık pozisyon varken karar döngüsü çalışmaz. Sadece stop/hedef takip edilir, böylece token harcanmaz.
- Aynı mumda hem stop hem hedef görülürse önce stop'a değdiği varsayılır (muhafazakâr). Fiyat boşlukla açılırsa işlem mumun açılış fiyatından kapanır.
- **Kill switch:** proje klasöründe `STOP` adlı bir dosya oluştur. Bot yeni işlem açmayı bırakır, açık pozisyonu da kapatır.

## Ölçüm: masa gerçekten işe yarıyor mu?

Bot iki ayrı paper hesap tutar:

- **Masa** (`data/desk.sqlite`): Altı ajanın kararları.
- **Baseline** (`data/baseline.sqlite`): LLM kullanmayan basit bir kural. `close > EMA20 > EMA50` ve RSI 45–70 olduğunda ya da RSI < 32 olduğunda, stop 1×ATR, hedef 3×ATR ile işlem açar. Risk kuralları masanınkiyle aynıdır. Masada açık pozisyon olsa bile baseline kendi işlemlerine devam eder.

**Karşı-olgusal takip (shadows):** Geçerli her setup kaydedilir, NOVA TRADE, WAIT ya da REJECT demiş olsun fark etmez. Sonra fiyatın önce stop'a mı yoksa hedefe mi değdiği izlenir. 72 saatte ikisine de değmezse o anki fiyattan değerlendirilir. Bu kayıtlardan iki şey çıkar:
- **Ajan isabeti:** Örneğin ORION SUPPORTIVE dediğinde hedefe ulaşma oranı, AGAINST dediğindekinden yüksek mi? Değilse ORION karara bilgi katmıyor demektir.
- **Seçim testi:** Aynı setup'lar içinde NOVA'nın aldıkları, almadıklarından daha mı iyi sonuç verdi? Piyasa koşullarının etkisini dışarıda bırakan en temiz test budur.

**Hüküm istatistiksel:** Masa ile baseline arasındaki R/işlem farkı için %95 bootstrap güven aralığı hesaplanır. Bu aralık sıfırı içeriyorsa sonuç "NO SIGNIFICANT DIFFERENCE" olur. Her iki tarafta da 20 işlem tamamlanana kadar sonuç "INSUFFICIENT SAMPLE" olarak kalır. Çevrimdışı bir simülasyonda ajanların yerine **rastgele karar veren** sahte ajanlar koydum. Tek bir dönemde baseline'dan daha fazla kazandılar, ama test bunu doğru şekilde "anlamlı fark yok" olarak işaretledi. Bu hüküm, şansla gelen iyi sonuçları ayıklamak için var.

```bash
python3 run.py eval        # JSON: masa/baseline, güven aralığı, shadow'lar, ajan isabeti, LLM harcaması
python3 run.py dashboard   # data/dashboard.html (Telegram ayarlıysa oraya da gönderir)
```

### İstatistikler ne kadar güvenilir? (kalibrasyon)

Bot çevrimdışı olarak, **rastgele karar veren** sahte ajanlarla, sentetik bir piyasada ve 40 farklı tohumla (SEED=1…40) çalıştırıldı (`tools/calibrate.py`). Rastgele ajanların hiçbir becerisi yok. Bu yüzden testlerin "bir şey bulduk" dediği her durum yanlış alarmdır:

| Test | Yanlış alarm (40 tohum) | Hedef |
|---|---|---|
| Masa vs baseline | 0 / 40 | ≤ %5 |
| Seçim testi (%95, gün kümeli) | 1 / 40 (%2,5) | ≈ %5 |
| Ajan değeri (%99, gün kümeli), test başına | 4 / 200 (%2) | ≈ %1 |
| Ajan değeri, en az bir ajanın işaretlendiği çalıştırma | 4 / 40 (%10) | ≈ %5 |

Ajan değeri testi hedefinden biraz daha sık yanlış alarm veriyor. 40 tohumluk bir ölçüm bu farkın kesin olduğunu göstermeye yetmez, ama temkinli ol: **tek bir raporda bir ajanın "ADDS INFORMATION" ya da "INVERSE SIGNAL" alması, o ajanı kapatmak ya da ona güvenmek için yeterli değildir.** Sonucun birkaç hafta boyunca tutarlı kalmasını bekle. Saatlik setup'lar birbirine bağımlı olduğu için güven aralıkları günlere göre kümelenerek hesaplanıyor (cluster bootstrap); beş ajan aynı anda test edildiği için %99 güven aralığı kullanılıyor.

Kalibrasyon tekrarlanabilir: aynı `SEED` her zaman aynı sonucu verir (sabit sentetik başlangıç zamanı, paralel ajanlar için ajan bazında RNG). Tekrar çalıştırmak için:

```bash
for i in $(seq 1 40); do SEED=$i python3 tools/calibrate.py; done | grep CAL
```

### Deneyler (experiment)

Her karar bir deney kimliğiyle kaydedilir. Bu kimlik prompt'lardan, risk kurallarından, modellerden, baseline ve ön filtre ayarlarından, piyasalardan oluşan bir parmak izidir. Bunlardan herhangi birini değiştirdiğinde **yeni bir deney başlar** ve `eval` ile `dashboard` yalnızca geçerli deneyi sayar. Böylece eski ve yeni ayarların sonuçları birbirine karışmaz. Tüm deneyleri birlikte görmek için `--all` kullan. İstersen `config.yaml` dosyasına `experiment_name: v2` gibi okunaklı bir ön ek ekleyebilirsin.

## Baseline kuralının geçmiş testi

```bash
python3 run.py backtest --days 180     # LLM yok, ücretsiz
```

Bu komut baseline kuralını geçmiş mumlar üzerinde, canlıdaki risk kurallarıyla çalıştırır. Aynı komisyon, kayma, stop-önce ve gap kurallarını, aynı R:R, boyut, günlük limit ve bekleme süresini kullanır. Sinyal yalnızca kapanmış mumlardan üretilir, giriş o mumun kapanışından yapılır, çıkış bir sonraki mumdan itibaren kontrol edilir. Bir test de geleceğe ait mumlar eklendiğinde geçmiş işlemlerin değişmediğini doğruluyor. Çıktıda ortalama R için %95 güven aralığı var.

**Bunu ilk iş olarak çalıştır.** Baseline kuralı geçmişte zarar ediyorsa ön filtre LLM'lere kötü adaylar gönderiyor demektir. Bu durumda önce kuralı düzelt, sonra LLM'lere para harca.

**LLM masası neden geçmişte test edilmiyor:** Model, geçmiş fiyat hareketlerini eğitim verisinden biliyor olabilir. Bu da "geleceği bilen" sahte bir başarı üretir. Ayrıca o tarihlere ait haber ve duygu verisi elimizde yok. Masa yalnızca ileriye dönük paper işlemlerle ölçülebilir.

## Sağlamlık

- **Ajan çıktısı doğrulama:** Bir ajan izin verilen değerlerin dışında bir şey dönerse (ör. NOVA "STRONG BUY" derse ya da TITAN düz metin döndürürse) bu en temkinli seçenek olarak yorumlanır (REJECT veya NEED MORE DATA) ve rapora `invalid_outputs` olarak işlenir.
- **LLM hatası:** API hatası ya da zaman aşımı durumunda o döngü `ERROR` olarak kaydedilir, işlem açılmaz ve harcanan token yine kaydedilir. Telegram uyarısı 1., 3. ve her 12. ardışık hatada gelir. Baseline hesabı aynı döngüde çalışmaya devam eder.
- **Veri kesintisi:** 3 döngü üst üste hiçbir piyasadan veri gelmezse Telegram uyarısı gönderilir.
- **Veri kalitesi:** NaN/sonsuz/sıfır/negatif fiyat, negatif hacim ya da tutarsız OHLC (ör. yüksek < kapanış) içeren bir mum serisinin tamamı reddedilir; o piyasa o döngüde atlanır. Tekrarlanan zaman damgaları ayıklanır, eksik mumlar `missing` alanına yazılır. DexScreener başka bir zincir veya havuz döndürürse istatistikler kullanılmaz.
- **Ağ hataları:** 429/5xx ve zaman aşımlarında en fazla 2 tekrar (1 sn, 2 sn; `Retry-After` en fazla 10 sn). Sonsuz döngü yok.
- **Sayısal doğrulama:** Fiyat, stop, hedef, komisyon, kayma ve hesap değerleri sonlu ve geçerli değilse risk motoru `inputs_valid` / `account_valid` kontrolüyle reddeder. Bilinmeyen değer hiçbir zaman olumlu sayılmaz.
- **Tek süreç kilidi:** `run.py once` ve `run.py loop` aynı anda çalışamaz (`data/solunaro.lock`). Servis açıkken `once` çalıştırırsan bot açıkça uyarır.
- **Yeniden başlatma:** Açık pozisyonlar, günlük limitler ve bekleme süresi SQLite'tan okunur. Kapanmış bir pozisyon ikinci kez kapanamaz, PnL iki kez yazılmaz.

## Maliyet düşürme

- **Ön filtre:** Baseline kuralının aday saymadığı piyasalar LLM'e hiç gönderilmez. ATLAS yalnızca uygun piyasaları görür. Bunu `prefilter.enabled: false` ile kapatabilirsin.
- **Ajan bazında model:** `llm.models` altında örneğin ATLAS ve LUNA'ya `claude-haiku-5-5` atayabilirsin.
- **Düşünme derinliği:** `llm.effort` (`low` … `max`). Varsayılan `medium`. Güncel modeller `temperature` kabul etmez, derinlik buradan ayarlanır. Değiştirmek yeni bir deney başlatır.
- **Ret durumu:** Model bir isteği reddederse ya da `max_tokens`'a takılırsa döngü `ERROR` olarak kaydedilir, işlem açılmaz. `llm.fallbacks: true` iken reddedilen istek sunucu tarafında başka bir modelle yeniden denenir; hangi modelin cevap verdiği token kaydına yazılır.
- **Gerçek harcama:** Her LLM çağrısının token sayısı veritabanına yazılır. `llm.prices_per_mtok` alanını doldurursan maliyet USD olarak da gösterilir. Fiyatları ben yazmadım, anthropic.com/pricing adresinden kendin gir.
- Prompt caching eklenmedi. Sistem prompt'ları yaklaşık 300–400 token, önbelleğe alınabilecek minimum boyutun altında kalıyor. Bu yüzden kazanç sağlamaz.

## Ek veri

- **LUNA:** Binance USDT-M funding oranı ve 24 saatlik open interest değişimi eklendi. Anahtar gerekmez. ABD IP'sinden erişim engellenebilir.
- **ORION:** İsteğe bağlı olarak CryptoPanic haberleri (oy sayılarıyla) eklenebilir. Bunun için `CRYPTOPANIC_TOKEN` ayarla ve `cryptopanic.enabled: true` yap. CryptoPanic'in API adresi geçmişte değişti. `run.py data` ile kontrol et, gerekirse `cryptopanic.url` değerini güncelle.

## Telegram (tek yönlü)

`.env` içine `TELEGRAM_BOT_TOKEN` ve `TELEGRAM_CHAT_ID` yazarsan bot şu durumlarda mesaj gönderir: her paper TRADE, kapanan her pozisyon, hatalar (saatte en fazla bir kez) ve her gün 18:00 UTC'de özet ile dashboard dosyası. Bot Telegram'dan komut almaz.

## Tek komutla kurulum (önerilen)

Zip dosyasını VPS'e yükle (örneğin Termius'un SFTP özelliğiyle), ardından:

```bash
unzip solunaro.zip && cd solunaro
sudo bash install.sh          # kurulum, testler ve veri kontrolü yapar, botu başlatmaz
sudo bash install.sh --start  # her şey yolundaysa botu sürekli çalıştırır
```

API anahtarını ilk kurulumda gizli olarak girersin; boş geçebilirsin (testler, veri kontrolü ve backtest anahtarsız çalışır). Anahtar sadece `/opt/solunaro/.env` dosyasına (izin 600) kaydedilir. Script'i tekrar çalıştırmak kodu günceller; **var olan `.env` dosyasına, verilere ve `STOP` dosyasına dokunmaz.** Servis çalışıyorsa güncelleme sırasında durdurulur ve testler geçerse yeniden başlatılır. Testler başarısız olursa kurulum hata koduyla durur.

Anahtarın doğru olup olmadığını token harcamadan kontrol et:

```bash
sudo -u solunaro bash -c 'cd /opt/solunaro && set -a && . ./.env && set +a && .venv/bin/python run.py keycheck'
```

Anthropic anahtarları `sk-ant-` ile başlar. console.anthropic.com'daki listede görünen `apikey_...` değeri anahtarın kimliğidir, anahtarın kendisi değildir.

**Yedekleme:** `sudo tar -C /opt/solunaro -czf ~/solunaro-yedek.tgz data .env` — geri yüklemek için servisi durdur, arşivi `/opt/solunaro` içine aç, `sudo chown -R solunaro:solunaro /opt/solunaro`.

## Kurulum (VPS, elle)

```bash
sudo useradd -r -m solunaro
sudo mkdir -p /opt/solunaro && sudo cp -r . /opt/solunaro && sudo chown -R solunaro /opt/solunaro
cd /opt/solunaro
pip install -r requirements.txt
cp .env.example .env && nano .env          # ANTHROPIC_API_KEY
nano config.yaml                           # model (varsayılan claude-opus-5-5) ve effort'u kontrol et

python3 run.py data      # LLM'siz veri testi, ücretsiz. Önce bunu çalıştır
python3 run.py keycheck  # API anahtarı geçerli mi (token harcamaz)
python3 run.py once      # tek tam döngü (6 ajan)
python3 run.py status    # paper bakiye, açık pozisyon, kararlar

sudo cp solunaro.service /etc/systemd/system/
sudo systemctl enable --now solunaro
journalctl -u solunaro -f
```

Her kararın tam raporu (tüm veri paketi ve 6 ajanın JSON çıktısı) `data/reports/` klasörüne kaydedilir. Özet bilgiler `data/desk.sqlite` içinde tutulur.

**Binance notu:** Bot önce `data-api.binance.vision` adresini, olmazsa `api.binance.com` adresini dener. ABD IP'li bir VPS'te Binance 451 hatası döndürebilir. `run.py data` bunu hemen gösterir.

## Maliyet

Bir tam döngü 6 LLM çağrısı demek. Saatte bir çalıştığında günde en fazla yaklaşık 144 çağrı yapar. Açık pozisyon varken ve veri eskiyken bu sayı sıfıra iner. Gerçek token kullanımı her raporun `llm_usage` alanında yazar.

Kaba tahmin (`claude-opus-5-5`, `effort: medium`, çağrı başına ~3 bin girdi + ~2 bin çıktı token'ı): döngü başına ~0,30 $, en kötü durumda (her saat aday var) günde ~7 $. Ön filtre çoğu saati elediği için gerçek harcama genelde bunun çok altında kalır. `run.py eval` gerçek USD harcamasını gösterir; tahmine değil ona bak.

## Testler

```bash
python3 -m unittest discover -s tests
```

68 çevrimdışı test var. Ağa ya da API anahtarına ihtiyaç duymazlar; ayrıntılar `docs/TESTING.md` içinde. Kapsadıkları konular: tam TRADE yolu, TITAN vetosunun NOVA'yı ezmesi, uydurma stop'un reddedilmesi, haber yoksa işlem olmaması, eski veride ve ön filtrede LLM'in çağrılmaması, kill switch, stop-önce kuralı ve PnL, API ayrıştırıcıları, baseline hesabı, shadow takibi, ajan isabeti, bootstrap hükmü, kümeli ajan değeri testi, deney filtresi ve eski veritabanı geçişi, ajan çıktısı doğrulama, LLM hatasında işlem açılmaması, veri kesintisi uyarısı, geçmiş testinde geleceğe bakmama, Binance sayfalama, token ve maliyet kaydı, CryptoPanic birleştirme, Telegram bildirimleri, günlük zamanlama ve dashboard; ayrıca NaN/sonsuz/negatif girdiler, bozuk ve tekrarlanan mumlar, HTTP 429 ve zaman aşımı, girişten sonraki ilk mumun kontrolü, yeniden başlatma sonrası limitler, çift kapanış, süreç kilidi ve maliyet sonrası R:R.

Mimari, risk modeli ve hazırlık durumu için: `docs/ARCHITECTURE.md`, `docs/RISK_MODEL.md`, `docs/READINESS.md`.

## Gerçek paraya geçmeden önce

Rehberin kuralı: en az 20 paper kararı, süreç değerlendirmesi ve her zaman insan onayı ile çalışan bir kill switch. Paper kazancı, gerçek parada da kazanılacağının kanıtı değildir. Bu proje bilinçli olarak emir gönderme kodu içermiyor.
