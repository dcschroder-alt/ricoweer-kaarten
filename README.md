# Ricoweer – automatische weerkaarten voor gratis WordPress.com

Dit pakket publiceert automatisch **twee losse PNG-bestanden** met de landelijke dagverwachting:

- `morgen.png` — weerkaart van morgen;
- `overmorgen.png` — weerkaart van overmorgen.

De afbeeldingen zijn bedoeld voor twee **Afbeelding**-blokken, onder elkaar, in een gratis WordPress.com-bericht. Bezoekers hoeven niet te klikken. De afbeeldinglinks verwijzen niet rechtstreeks naar de hoofdapp.

## Wat zit er in het pakket?

```text
ricoweer-kaarten/                (nieuwe losse GitHub-repository)
├── .github/
│   └── workflows/
│       └── publiceer-kaarten.yml
├── scripts/
│   └── maak_kaarten.py
├── requirements.txt
├── WORDPRESS-LINKS.txt
├── .gitignore
└── README.md
```

**Na een geslaagde automatische uitvoering** publiceert GitHub Pages alleen deze bestanden:

```text
public/                           (wordt automatisch gegenereerd)
├── morgen.png
├── overmorgen.png
└── index.html                    (kleine voorbeeldpagina met alleen beide PNG's)
```

De map `public` hoeft niet vooraf op GitHub te staan. Het bestand `weerkaart.html` hoort **niet** in deze aparte publieke repository.

## Eerste installatie (één keer)

1. Controleer of `https://dcschroder-alt.github.io/Regioweer/weerkaart.html` rechtstreeks in je browser **twee actuele dagkaarten** toont. Zo niet: plaats `weerkaart.html` uit **Ricoweer v100.7** eerst in je oorspronkelijke `Regioweer`-repository. Deze bronpagina is noodzakelijk voor de PNG-export. De exporter berekent geen alternatieve, afwijkende modelmix.
2. Maak op GitHub een nieuwe **openbare** repository met exact de naam **`ricoweer-kaarten`** (geen spaties, helemaal klein geschreven). De oorspronkelijke `Regioweer`-repository laat je ongemoeid.
3. Pak deze ZIP op je Windows-pc uit. Upload de inhoud, inclusief de **verborgen `.github`-map met de workflow**, naar de hoofdmap van de nieuwe repository. Controleer in GitHub of `.github/workflows/publiceer-kaarten.yml` werkelijk bestaat. Gebruik bij voorkeur GitHub Desktop of upload de mappenstructuur; de root van de nieuwe repository mag niet een extra overkoepelende ZIP-map bevatten.
4. Ga in de **nieuwe** repository naar **Settings → Pages → Build and deployment → Source → GitHub Actions**. Bewaar de instelling.
5. Ga naar **Actions → Ricoweer - twee kaarten voor WordPress → Run workflow → Run workflow**. Dit start de eerste publicatie. De workflow controleert of er actuele daggegevens en kaarttegels zijn. Als de workflow groen is, verschijnen de bestanden via GitHub Pages.
6. Open de twee onderstaande afbeeldingslinks rechtstreeks. Controleer of de kaarten Nederland goed tonen én werkelijk de dagen morgen/overmorgen bevatten.

## Vaste WordPress-afbeeldingslinks

Na succesvolle eerste publicatie:

**Morgen**  
`https://dcschroder-alt.github.io/ricoweer-kaarten/morgen.png`

**Overmorgen**  
`https://dcschroder-alt.github.io/ricoweer-kaarten/overmorgen.png`

In je **gratis WordPress.com**-artikel:

1. Verwijder het bestaande kapotte afbeeldingsblok met de `weerkaart.html`-bestandsnaam.
2. Voeg een **Afbeelding**-blok toe, kies **Invoegen vanaf URL**, plak de link van `morgen.png` en bevestig.
3. Voeg **daaronder** een tweede Afbeelding-blok toe en plak de link van `overmorgen.png`.
4. Bewaar/publiceer je artikel. **Niet** kiezen voor een nieuwe upload naar de WordPress-mediabibliotheek, want die kopie zou statisch zijn en niet automatisch vernieuwen.

De bestandsnamen en URL's blijven dagelijks gelijk; de inhoud van de PNG verandert na nieuwe publicaties. Soms duurt het enkele minuten voordat browser/CDN een bijgewerkte versie toont.

## Automatische verversing

De workflow gebruikt de tijdzone **Europe/Amsterdam**. De geplande publicatietijden zijn het hele jaar **06:41, 10:41 en 14:41 uur Nederlandse tijd**, ook bij de overgang tussen wintertijd en zomertijd.
GitHub Actions kan een geplande taak vertraagd starten of incidenteel overslaan. Bij **publieke GitHub-repositories** kunnen geplande workflows na **60 dagen zonder repository-activiteit** automatisch worden uitgeschakeld. Controleer dus af en toe het Actions-overzicht en schakel de workflow opnieuw in als deze is uitgeschakeld. Gebruik zo nodig **Run workflow** voor een extra update.

**Let op:** tot de eerste geslaagde ochtendrun na middernacht kunnen de twee PNG-links nog de kaarten van de voorgaande kalenderdag tonen. De werkelijke datums staan in de kaartkop. Een 24/7-garantie van ononderbroken actualiteit bestaat niet met deze gratis publicatieroute.

## Kwaliteit en gegevens

- De kaart wordt gefotografeerd via een headless browser van de bestaande Ricoweer-pagina. **Dag-TX/TN worden niet vervangen door uurwaarden.** De bestaande modelmix, 5 zonklassen, weersymbolen en windlabels worden behouden.
- Kaarten krijgen een brede exportuitsnede (ongeveer 1000 pixels CSS-breedte, met hoge-resolutieschermafdruk) zodat veel Nederlandse punten leesbaar blijven.
- Alleen als de modelmix ten minste twee modellen bevat, beide dagen in de nieuwste scan staan en ieder minstens twaalf geldige scanpunten heeft, gaat het proces verder.
- Zonder geladen kaarttegels, zonder voldoende kaartpunten of bij een fout stopt de workflow vóór de GitHub Pages-publicatie. De vorige gepubliceerde afbeeldingen blijven dan staan totdat een volgende run wél slaagt.
- PNG's bevatten de vermelding `Kaart © OpenStreetMap-bijdragers`.

## Privacy en veiligheid

De **nieuwe openbare repository** bevat alleen deze exporter en, via Pages, de twee gepubliceerde weerkaarten. Bezoekers van WordPress hoeven niet naar je hoofdproject te gaan.

**Dit maakt een al openbare hoofdrepository niet privé.** Wie je GitHub-gebruikersnaam kent, kan eventueel andere openbare repositories terugvinden. Laat geen API-sleutels, wachtwoorden of privégegevens achter in een openbare repository. Voor daadwerkelijke afscherming van de oorspronkelijke app is een afzonderlijke host-/privacy-oplossing nodig.

## Problemen oplossen

- **404 bij de PNG:** de eerste workflow is nog niet geslaagd, Pages staat niet op `GitHub Actions`, of de naam van de repository wijkt af.
- **Workflow vindt geen kaarten:** controleer eerst `weerkaart.html` in de originele Ricoweer Pages-site. De exporter kan die niet zelf publiceren.
- **Te weinig punten of modellen:** de bron-API was tijdelijk niet beschikbaar. De nieuwe PNG wordt uit veiligheid niet gepubliceerd. Probeer later handmatig `Run workflow`.
- **PNG verandert niet:** kijk in GitHub → Actions naar de laatste succesvolle uitvoering; controleer daarna browsercache / CDN-vertraging en of WordPress een afbeelding **vanaf URL** toont in plaats van een opgeslagen kopie.

Deze set is getest met een **gesimuleerde pagina en testgegevens** voor de exportlogica en afbeeldingslayout. De echte publicatie op GitHub en de live Open-Meteo/OSM-gegevens moeten na installatie worden gecontroleerd.
