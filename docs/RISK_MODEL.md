# Risk modeli

Bu kurallar `desk/risk.py` içinde koddur. LLM'ler (TITAN dahil) bunları gevşetemez; TITAN yalnızca reddedebilir. Miktar, stop, hedef ve fiyat LLM'den değil koddan gelir.

## Girdi doğrulama (fail-closed)

| Kontrol | Reddedilen durum |
|---|---|
| `inputs_valid` | Fiyat, stop veya hedef NaN, ±sonsuz, 0 ya da negatif; komisyon/kayma negatif veya sonlu değil |
| `account_valid` | Bakiye ≤ 0 veya sonlu değil; günlük PnL ya da veri yaşı bilinmiyor |
| `setup_numbers` | Stop/hedef yok veya sayı değil |

Bilinmeyen değer hiçbir zaman nötr ya da olumlu sayılmaz.

## Setup kontrolleri

- `long_only_geometry`: stop < giriş < hedef (yalnızca spot long).
- `stop_grounded_in_data`, `target_grounded_in_data`: seviye, sunulan verideki bir değerle %0,1 içinde eşleşmeli: EMA20/50/200, 24s ve dönem yüksek/düşük, swing destek/direnç, son 12 mumun yüksek/düşükleri, `son_kapanış ± k×ATR14` (k ∈ 1; 1,5; 2; 2,5; 3). Baseline'ın stop/hedefi aynı `son_kapanış ± k×ATR14` tanımını kullanır.
- `stop_distance` ≤ %8, `target_distance` ≤ %25.
- `reward_risk` ≥ 2, **maliyetler sonrası**:
  - `maliyet = 2×komisyon + 2×kayma` (giriş ve çıkış)
  - `giriş = fiyat × (1 + kayma)`
  - `risk = giriş − stop + giriş × maliyet`
  - `ödül = hedef − giriş − giriş × maliyet`

## Hesap kontrolleri

- `max_open_positions` (1), `max_trades_per_day` (5), `daily_kill` (günlük gerçekleşmiş zarar < %1), `cooldown` (aynı piyasa 180 dk), `data_fresh` (≤ 90 dk).
- Hepsi SQLite'tan okunur; süreç yeniden başladığında korunur.

## Boyut

`miktar = (bakiye × %0,25) / risk_birim`, nominal değer bakiyenin %20'siyle sınırlanır. Sonuçta `max_loss_usd ≤ bakiye × %0,25` (komisyon ve kayma dahil, fiyat boşluğu hariç).

## Paper dolum varsayımları ve sınırlamaları

- **Giriş:** Son *kapanmış* mumun kapanışı + kayma. Karar o mumun kapanışından en fazla 90 dakika sonra verilebilir; bu arada fiyat değişmiş olabilir. Bu bir iyimserlik kaynağıdır ve gerçek dolumu temsil etmez.
- **Çıkış kontrolü:** Giriş mumunun kapanışından (`entry_ts`) sonra açılan her kapanmış mum kontrol edilir; backtest ile aynı tanım. (Bu sürümden önce, giriş sonrası ilk mum atlanıyordu.)
- **Aynı mumda stop ve hedef:** Önce stop varsayılır (muhafazakâr).
- **Fiyat boşluğu:** Mum stop'un altında açılırsa çıkış açılış fiyatından; hedefin üstünde açılırsa açılıştan.
- **Kayma** çıkışta da uygulanır; komisyon giriş ve çıkış nominaline uygulanır.
- **Kill switch:** Yeni giriş yapılmaz; açık pozisyon son kapanmış mumun kapanışından kapatılır.
- Açık pozisyonun gerçekleşmemiş zararı günlük zarar limitine sayılmaz.
- Likidite etkisi (büyük emirde fiyat kayması) modellenmez; DEX için sabit %0,30 komisyon + %0,50 kayma varsayılır (Meteora gibi dinamik ücretli havuzlarda gerçek ücret farklı olabilir).
