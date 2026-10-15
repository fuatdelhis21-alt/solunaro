# Test

Tüm testler çevrimdışıdır: ağ, API anahtarı, cüzdan ya da ücretli LLM çağrısı gerekmez. LLM yerine betikli sahte ajanlar (`tests/test_desk.py::FakeLLM`) kullanılır.

```bash
python3 -m unittest discover -s tests      # 68 test
python3 -m compileall -q .
for i in $(seq 1 40); do SEED=$i python3 tools/calibrate.py; done | grep CAL   # kalibrasyon
```

| Dosya | Kapsam |
|---|---|
| `test_desk.py` | İndikatörler, risk kuralları, stop-önce/boşluk, tam TRADE yolu, TITAN vetosu, uydurma seviye, haber şartı, ATLAS hatası, eski veri, kill switch |
| `test_measurement.py` | Baseline, ön filtre, shadow'lar, eval hükmü, düşüş, harcama, Telegram, dashboard |
| `test_robustness.py` | Çıktı doğrulama, LLM hatası, veri kesintisi, deney kimliği, şema geçişi, ajan değeri, backtest'te geleceğe bakmama, sayfalama |
| `test_parsers.py` | Binance/GeckoTerminal/DexScreener/RSS ayrıştırma, sembol eşleşmesi |
| `test_llm_client.py` | İstek gövdesi (effort, fallback), ret ve `max_tokens`, cevap veren modelin kaydı, haber şartı |
| `test_audit.py` | NaN/sonsuz/sıfır/negatif girdiler, maliyet sonrası R:R, bozuk/tekrarlanan mumlar, yanlış havuz, HTTP 429/5xx/zaman aşımı, giriş sonrası ilk mum, çift kapanış, yeniden başlatma, şema geçişi, shadow geçerliliği, makine okunur ret nedeni, gecikme metriği, süreç kilidi |

## Kalibrasyon ne ölçer?

`tools/calibrate.py` botun tamamını sentetik bir piyasada **rastgele** ajanlarla çalıştırır. Rastgele ajanların becerisi yoktur; istatistiksel testlerin "anlamlı" dediği her sonuç yanlış alarmdır. Sonuçlar gerçek performans hakkında **hiçbir şey** söylemez. Güncel ölçümler README'deki tabloda.

## Kapsanmayan / elle doğrulanması gerekenler

- Gerçek API yanıt biçimleri yalnızca kayıtlı örneklerle test edilir; sağlayıcı formatı değişirse `run.py data` ile görülür.
- `install.sh` ve systemd servisi otomatik test edilmez; gerçek bir sunucuda elle denenmelidir.
- Gerçek Anthropic API çağrısı test edilmez; `run.py keycheck` ücretsiz bir anahtar kontrolü yapar, `run.py once` ilk ücretli döngüdür.
