# Pozemky Prešov

Denný tracker stavebných pozemkov okolo Prešova, Sabinova a PNV.

Zdroje: Bazoš, Nehnuteľnosti.sk, RealitySpolu (zahŕňa aj Reality.sk, TopReality.sk a väčšinu realitiek
ako Reality Hofer), KingReal, ZOOM Reality. Ďalšia realitka sa pridá 4 riadkami v `config.yaml`.
Beží zadarmo na GitHub Actions, o 8:00 a 17:30 pošle e-mail s novinkami a znížením cien,
prehľad so stavmi (volané, obhliadka…) je na GitHub Pages.

## Nastavenie (cca 15 min, raz)

1. **GitHub** – vytvor repozitár `pozemky` (Public – Pages sú pre verejné repo zadarmo;
   e-maily a heslo budú v tajných Secrets, nie v kóde). Nahraj doň obsah tohto priečinka
   vrátane priečinka `.github`.
2. **Gmail heslo aplikácie** – na Gmail účte, z ktorého sa bude posielať, zapni dvojfázové
   overenie a vytvor heslo aplikácie: myaccount.google.com/apppasswords
3. **Secrets** – v repozitári Settings → Secrets and variables → Actions → New repository secret:
   - `SMTP_USER` – Gmail adresa odosielateľa
   - `SMTP_PASS` – heslo aplikácie (16 znakov, bez medzier)
   - `MAIL_TO` – príjemcovia oddelení čiarkou
4. **Prehľad** – Settings → Pages → Deploy from a branch → `main` / `/docs`.
   Adresu stránky ulož v záložke Variables ako `PAGES_URL` (odkaz sa objaví v e-maile).
5. **Test** – Actions → „Pozemky – denný beh“ → Run workflow. Prvý e-mail obsahuje
   celú aktuálnu ponuku, ďalšie už len novinky. Potom to beží samo každé ráno.

## Úpravy
Všetko je v `config.yaml`: obce a zóny, blacklist, max. cena, min. výmera, zdroje.

## Keď príde e-mail s ⚠
Niektorý portál zmenil web a vrátil 0 inzerátov. Stačí parser opraviť (`scraper.py`).

## Lokálny test
```
pip install -r requirements.txt
python scraper.py --fixtures tests/   # offline na ukážkových dátach
python scraper.py --dry-run           # ostrý zber, e-mail len do email_preview.html
```
