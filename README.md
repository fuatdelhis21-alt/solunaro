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
- İlgili güncel haber yoksa işlem açılmaz (`min_news_items`).
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

Botu çevrimdışı olarak, **rastgele karar veren** sahte ajanlarla ve 40 farklı rastgele tohumla çalıştırdım (`tools/calibrate.py`). Rastgele ajanların hiçbir becerisi yok. Bu yüzden testlerin "bir şey bulduk" dediği her durum yanlış alarmdır:

| Test | Yanlış alarm | Hedef |
|---|---|---|
| Masa vs baseline | 0 / 40 | ≤ %5 |
| Seçim testi (%95) | 2 / 40 (%5) | ≈ %5 |
| Ajan değeri (%99, gün kümeli) | 2 / 160 (%1,25) | ≈ %1 |

İlk sürümde ajan değeri testi rastgele bir LUNA'yı "anlamlı" buldu. Bunun sebebi, saatlik setup'ların birbirine bağımlı olması: aynı gün açılan setup'lar çoğunlukla birlikte sonuçlanıyor. Düzeltmek için güven aralıklarını günlere göre kümeleyerek hesaplıyorum (cluster bootstrap). Beş ajan aynı anda test edildiği için de %95 yerine %99 güven aralığı kullanıyorum. Yine de tek bir raporda beş ajandan birinin yanlışlıkla işaretlenme olasılığı yaklaşık %6. Bir ajanı kapatmadan önce sonucun birkaç hafta boyunca tutarlı kaldığını gör.

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

## Maliyet düşürme

- **Ön filtre:** Baseline kuralının aday saymadığı piyasalar LLM'e hiç gönderilmez. ATLAS yalnızca uygun piyasaları görür. Bunu `prefilter.enabled: false` ile kapatabilirsin.
- **Ajan bazında model:** `llm.models` altında örneğin ATLAS ve LUNA'ya daha ucuz bir model atayabilirsin.
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

API anahtarını kurulum sırasında gizli olarak girersin. Anahtar sadece `/opt/solunaro/.env` dosyasına (izin 600) kaydedilir. Script'i tekrar çalıştırmak kodu günceller, `.env` dosyasına ve verilere dokunmaz.

## Kurulum (VPS, elle)

```bash
sudo useradd -r -m solunaro
sudo mkdir -p /opt/solunaro && sudo cp -r . /opt/solunaro && sudo chown -R solunaro /opt/solunaro
cd /opt/solunaro
pip install -r requirements.txt
cp .env.example .env && nano .env          # ANTHROPIC_API_KEY
nano config.yaml                           # llm.model'i güncel modelle değiştir

python3 run.py data      # LLM'siz veri testi, ücretsiz. Önce bunu çalıştır
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

## Testler

```bash
python3 -m unittest discover -s tests
```

40 çevrimdışı test var. Ağa ya da API anahtarına ihtiyaç duymazlar. Kapsadıkları konular: tam TRADE yolu, TITAN vetosunun NOVA'yı ezmesi, uydurma stop'un reddedilmesi, haber yoksa işlem olmaması, eski veride ve ön filtrede LLM'in çağrılmaması, kill switch, stop-önce kuralı ve PnL, API ayrıştırıcıları, baseline hesabı, shadow takibi, ajan isabeti, bootstrap hükmü, kümeli ajan değeri testi, deney filtresi ve eski veritabanı geçişi, ajan çıktısı doğrulama, LLM hatasında işlem açılmaması, veri kesintisi uyarısı, geçmiş testinde geleceğe bakmama, Binance sayfalama, token ve maliyet kaydı, CryptoPanic birleştirme, Telegram bildirimleri, günlük zamanlama ve dashboard.

## Gerçek paraya geçmeden önce

Rehberin kuralı: en az 20 paper kararı, süreç değerlendirmesi ve her zaman insan onayı ile çalışan bir kill switch. Paper kazancı, gerçek parada da kazanılacağının kanıtı değildir. Bu proje bilinçli olarak emir gönderme kodu içermiyor.
