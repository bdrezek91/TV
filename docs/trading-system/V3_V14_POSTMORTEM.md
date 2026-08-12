# Postmortem badań V3–V14

> Status: zamknięte badania na zaobserwowanych danych. Żaden wynik opisany w
> tym dokumencie nie stanowi zgody na handel produkcyjny ani na zmianę progów
> po wyniku.

## Werdykt

Rodzina jednoinstrumentowych strategii kierunkowych V3–V14 nie wykazała
stabilnej przewagi po kosztach. Nie należy ratować jej przez wybór symbolu,
godzin, kierunku lub cadence po zobaczeniu wyników.

| Wersja | Hipoteza | Najważniejszy wynik | Decyzja |
|---|---|---|---|
| V3 | izolacja setupów | trend pullback i breakout obniżały wynik MR | zamknięta |
| V5 | failed breakout/fakeout | expectancy od -0,235 do -0,340 R | odrzucona |
| V6 | momentum continuation | brak stabilności; dwa harmonogramy ujemne | odrzucona |
| V7 | CVD divergence | duże próby i ujemne 95% CI | odrzucona |
| V8 | VWAP reversion | -0,257 do +0,023 R zależnie od cadence | odrzucona |
| V9 | volatility expansion | 7–19 transakcji, niestabilny znak wyniku | odrzucona |
| V10 | trade-flow impulse | nie osiągnęła PF 1,15 | odrzucona |
| V11 | basis dislocation | tylko 2–10 transakcji, wszystkie expectancy ujemne | odrzucona |
| V12 | volume-weighted TSMOM | expectancy ujemne w każdym harmonogramie | odrzucona |
| V13 | gated breakout | tylko 3–6 transakcji | odrzucona |
| V14 | trend breakout | -0,461 / +0,237 / -0,080 R | odrzucona |

V4 było analizą forensic Mean Reversion, a nie niezależnym kandydatem.

## Mean Reversion

MR V1 przegrał zamrożony holdout 90d mimo wystarczającej próby 97–153
transakcji. Expectancy wyniosła 0,0005–0,0396 R, PF 1,00–1,15, każdy 95% CI
obejmował zero, a około 95% dodatniego PnL pochodziło z SOL.

MR V2 na danych development osiągnął dodatnie expectancy i PF w trzech
harmonogramach, ale miał tylko 11–22 transakcje, wszystkie CI obejmowały zero,
a wynik był nadmiernie skoncentrowany na BNB. Formalny wynik pozostaje
`HOLDOUT_FAIL`. Zarezerwowany holdout `holdout_mrv2_120d` pozostaje zamknięty.

## Konsekwencje dla V15

- Nie zmieniać progów ani nie wybierać strony SHORT z V14 po wyniku.
- Nie ponawiać jednoinstrumentowego breakout, momentum, VWAP/CVD reversal ani
  klasycznego Mean Reversion.
- Nie używać holdoutu zarezerwowanego dla MR V2 do oceny V15.
- Zmienić jednostkę hipotezy z pojedynczej pozycji na zsynchronizowaną parę
  alt/BTC o ograniczonej ekspozycji na ruch całego rynku.

