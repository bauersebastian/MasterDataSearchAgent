sap.ui.define([
  "sap/ui/core/mvc/Controller",
  "sap/ui/core/routing/History",
  "sap/ui/model/json/JSONModel"
], (Controller, History, JSONModel) => {
  "use strict";

  // help texts (HTML for sap.m.FormattedText: p, ul/li, strong, em, code)
  const SECTIONS = [
    {
      title: "Überblick",
      html: `<p>Die App durchsucht den SAP-Materialstamm (MARA, MAKT, MARM, MECL, MEME, MTXT, EINA sowie die Stücklisten
        STKO/STPO). Suchbegriff eingeben, Suchmodus wählen und mit Enter suchen. Ein Klick auf einen Treffer öffnet die
        Detailseite des Materials.</p>
        <p>Unabhängig vom Modus werden <strong>exakte Schlüssel</strong> immer zuerst gefunden: Materialnummer (mit oder ohne
        führende Nullen), EAN, Lieferanten-Teilenummer und alte Materialnummer. Sie erscheinen mit dem Kennzeichen
        <em>Exakt</em> ganz oben.</p>`
    },
    {
      title: "Suchmodus „Unscharf“ – Textsuche mit Tippfehler-Toleranz",
      html: `<p>Sucht nach den <strong>Wörtern</strong> in Kurztexten, Langtexten, Klassen, Merkmalen und Nummern
        (Verfahren BM25, ein wahrscheinlichkeitsbasiertes Ranking). Jedes Suchwort darf abweichen:</p>
        <ul>
          <li><strong>Tippfehler:</strong> <em>mesing</em> findet <em>Messing</em></li>
          <li><strong>Wortanfang:</strong> <em>Verschr</em> findet <em>Verschraubung</em> (auch während der Eingabe als Vorschlag)</li>
          <li><strong>Wortteil:</strong> <em>mutter</em> findet <em>Gegenmutter</em></li>
          <li><strong>Schreibweisen:</strong> <em>Duebel</em> = <em>Dübel</em>, <em>6x45</em> = <em>6 x 45</em></li>
        </ul>
        <p>Materialien, die <strong>alle</strong> Suchwörter enthalten, stehen oben. Bei korrigierten Wörtern bietet die App
        „Meinten Sie …“ an; das Symbol <em>?</em> in der Trefferliste zeigt, wie jedes Suchwort erweitert wurde.
        Am besten geeignet für Artikelnummern, Abmessungen und Herstellerbezeichnungen.</p>`
    },
    {
      title: "Suchmodus „Semantisch“ – Suche nach Bedeutung",
      html: `<p>Sucht nach der <strong>Bedeutung</strong> statt nach Wörtern. Jedes Material ist als Zahlenvektor
        (Embedding, 1536 Dimensionen) in einer Vektordatenbank gespeichert; die Anfrage wird genauso umgewandelt und die
        Materialien mit der ähnlichsten Bedeutung werden gefunden.</p>
        <ul>
          <li><em>Schraubenzieher</em> findet <em>Schraubendreher</em> (Synonym)</li>
          <li><em>Befestigung für Porenbeton</em> findet <em>Gasbetondübel</em> (Beschreibung)</li>
          <li><em>Pizza mit Käse</em> findet Fertigprodukte über ihre Stücklisten-Komponenten</li>
        </ul>
        <p>Der Prozentwert ist die Ähnlichkeit der Bedeutung. Feine Unterschiede wie <em>M20</em> und <em>M25</em> kann
        die semantische Suche kaum unterscheiden – dafür ist die unscharfe Textsuche da.</p>`
    },
    {
      title: "Suchmodus „Hybrid“ (Standard) – das Beste aus beiden",
      html: `<p>Führt die unscharfe Textsuche und die semantische Suche gleichzeitig aus und kombiniert beide Ranglisten
        (Reciprocal Rank Fusion): Materialien, die in <strong>beiden</strong> Listen weit oben stehen, landen ganz oben.
        Die Spalte <em>Treffer über</em> zeigt, welche Suche ein Material gefunden hat und mit welcher Bewertung.</p>
        <p>Für die meisten Anfragen die beste Wahl.</p>`
    },
    {
      title: "Suchmodus „KI-Suche“ – Anfragen mit Anforderungen",
      html: `<p>Für Anfragen in eigenen Worten mit Bedingungen, z. B. <em>Veganes Waschmittel ohne Mikroplastik mit
        Inhaltsstoffen aus EU-Anbau</em>. Ablauf:</p>
        <ul>
          <li><strong>1. Verstehen:</strong> das LLM erkennt das gesuchte Produkt, Suchbegriffe und die Anforderungen
          (angezeigt unter <em>Verstanden als</em>).</li>
          <li><strong>2. Kandidaten:</strong> die Hybrid-Suche liefert mit den Suchbegriffen 30 Kandidaten.</li>
          <li><strong>3. Prüfen:</strong> das LLM prüft jeden Kandidaten gegen jede Anforderung –
          <strong>ausschließlich anhand der Stammdaten</strong>, ohne allgemeines Produktwissen.</li>
        </ul>
        <p>Je Anforderung: <strong>✓ belegt</strong> (steht in den Stammdaten), <strong>? nicht belegt</strong> (keine Angabe),
        <strong>✗ widerspricht</strong> (Stammdaten sagen das Gegenteil). Angezeigt werden nur passende und teilweise
        passende Materialien – ein leeres Ergebnis bedeutet, dass der Materialstamm nichts Passendes enthält.
        Eine KI-Suche dauert einige Sekunden.</p>`
    },
    {
      title: "Relevanz, Kennzeichen und Filter",
      html: `<ul>
          <li><strong>Relevanz:</strong> im Modus Unscharf bzw. Semantisch die Bewertung dieser Suche, im Modus Hybrid die
          kombinierte Rangfolge, in der KI-Suche die Bewertung des LLM.</li>
          <li><strong>Stückliste / in n Stücklisten:</strong> das Material hat eine Stückliste bzw. ist Komponente anderer Stücklisten.</li>
          <li><strong>Filter:</strong> Materialart, Warengruppe, Lieferant; Materialien mit Löschvormerkung werden nur mit
          <em>Gelöschte einbeziehen</em> angezeigt.</li>
        </ul>`
    },
    {
      title: "Materialdetails: Stücklisten, Dubletten, An SAP senden",
      html: `<ul>
          <li><strong>Stücklisten:</strong> der Beziehungsgraph zeigt links, in welchen Stücklisten das Material verwendet
          wird (über alle Stufen), rechts seine Komponenten. Darunter die mehrstufige Strukturstückliste und der
          Verwendungsnachweis. Filter nach Verwendung und Werk.</li>
          <li><strong>Dubletten:</strong> Kandidaten aus semantischer Nähe, Namensähnlichkeit und gleichen Schlüsseln (EAN,
          Lieferanten-Teilenummer). <em>KI-Bewertung</em> lässt das LLM jeden Kandidaten als Dublette, mögliche Dublette,
          Variante oder verschieden einstufen.</li>
          <li><strong>An SAP senden:</strong> sendet das Material unverändert, wie es im Datenbestand gespeichert ist (alle
          Tabellen, bei Stücklistenköpfen inkl. STKO/STPO), mit der Materialnummer als <code>MATNR</code> an SAP – das
          vorhandene Material wird geändert. Vorher wird die XML-Nachricht angezeigt und eine Bestätigung abgefragt.</li>
          <li><strong>Suchdokument:</strong> der Text, der für die semantische Suche in die Vektordatenbank eingebettet wurde.</li>
          <li><strong>Lieferant</strong> in den Einkaufsinfosätzen: öffnet den Geschäftspartner.</li>
        </ul>`
    },
    {
      title: "Geschäftspartnersuche",
      html: `<p>Mit dem Schalter <strong>Material | Geschäftspartner</strong> über dem Suchfeld wechseln Sie zwischen den
        beiden Stammdatenobjekten; jede Suche behält ihre letzte Anfrage. Die Geschäftspartnersuche durchsucht den
        GPA-Extrakt (Allgemeine Daten, Rollen, Adressen und Kommunikation, Beziehungen / Ansprechpartner, Kunden- und
        Lieferantendaten, Bankverbindungen, Steuer- und Identifikationsnummern) mit denselben Suchmodi:</p>
        <ul>
          <li><strong>Exakt:</strong> Partner-, Kunden-, Lieferantennummer, externe Partnernummer, Steuernummer / USt-ID,
          IBAN, Identifikationsnummer, E-Mail, Telefon, Fax, Webseite.</li>
          <li><strong>Unscharf:</strong> Namen, Suchbegriffe, Adressen, Kommunikationsdaten und Nummern mit Tippfehler-Toleranz.</li>
          <li><strong>Semantisch:</strong> zusätzlich die Namen der Ansprechpartner (bzw. die Unternehmen einer Person) und
          bei Lieferanten die <strong>gelieferten Materialien</strong> aus den Einkaufsinfosätzen – <em>Lieferant für
          Kabelverschraubungen</em> findet Lieferanten, auch wenn das Wort nicht im Namen steht.</li>
          <li><strong>KI-Suche:</strong> z. B. <em>Lieferant für Akkuschrauber in Unterfranken mit E-Mail-Adresse</em> – das
          LLM prüft Rolle, Region (aus PLZ und Ort), Kommunikation usw. ausschließlich anhand der Stammdaten.</li>
          <li><strong>Filter:</strong> Partnertyp (Organisation, Person, Gruppe), Rolle (Kunde, Lieferant, Kontaktperson …),
          Land der Standardadresse, Gruppierung.</li>
        </ul>
        <p>Die Detailseite zeigt Adressen mit Kommunikationsdaten, den Beziehungsgraphen mit den Ansprechpartnern
        (Klick öffnet den Partner), die gelieferten Materialien (Klick öffnet das Material) und alle Tabellen des
        Extrakts unverändert.</p>
        <p><strong>An SAP senden</strong> sendet den Geschäftspartner wie ein Material unverändert an SAP: je Tabelle
        (<code>G000</code>, <code>G100</code>, <code>GADC</code>, …) eine Kopfzeile mit den SAP-Feldnamen und eine Zeile je
        Datensatz, jede Zeile mit der Partnernummer als <code>PARTNER</code>. Vorher wird die XML-Nachricht angezeigt und
        eine Bestätigung abgefragt.</p>
        <p><strong>Löschprüfung</strong> prüft auf Knopfdruck (nur lesend, live aus SAP), ob noch offene Belege den
        Partner verwenden – in <em>jeder</em> Partnerrolle (Auftraggeber, Warenempfänger, Rechnungsempfänger, Regulierer):
        nicht erledigte Kundenaufträge, gültige Angebote (abgelaufene nur als Hinweis; vollständig abgelehnte Belege generell nur als Hinweis), Fakturen, die noch nicht an die
        Buchhaltung übergeben sind, Lieferungen ohne Warenausgang, nicht erledigte Kontrakte und Lieferpläne (abgelaufene
        nur als Hinweis), Retouren und Gutschriftsanforderungen, dazu offene Posten des Debitoren- bzw. Kreditorenkontos
        (FI, alle Buchungskreise, mit Saldo; Merkposten wie Anzahlungsanforderungen nur als Hinweis).</p>
        <p>Bei Lieferanten (als Lieferant, Rechnungssteller, Warenlieferant, Bestelladresse oder Bieter): nicht gebuchte
        (vorerfasste) Eingangsrechnungen, Bestellungen mit nicht vollständig gelieferten Positionen, nicht gelöschte
        Lieferpläne und Kontrakte des Einkaufs (abgelaufene nur als Hinweis) und Anlieferungen ohne Wareneingang. Nur als
        Hinweis: offene Bestellanforderungen, offene Anfragen und die Einkaufsinfosätze laut Extrakt.</p>
        <p>Bereiche, deren Schnittstelle nicht erreichbar ist, erscheinen als <em>nicht geprüft</em> – das Ergebnis ist
        dann „unvollständig“, nie „in Ordnung“. Hinweise verhindern das Löschen nicht.</p>`
    },
    {
      title: "Dublettenprüfung für Geschäftspartner",
      html: `<p>Kandidaten aus semantischer Nähe, Namensähnlichkeit <strong>ohne Rechtsform</strong> (<em>GmbH &amp; Co.KG</em>
        = <em>KG</em>), <strong>gleicher Adresse</strong> und <strong>gemeinsamen Schlüsseln</strong>: Steuernummer / USt-ID,
        IBAN, Identifikationsnummer (stark), E-Mail, Telefon, Fax, Webseite (schwächer). Verweist der Kunde auf die
        Lieferantennummer eines anderen Partners (oder umgekehrt), ist das ein starkes Indiz – in S/4HANA sollte ein
        Partner beide Rollen tragen. Gleicher Name an einem <em>anderen Ort</em> ist meist eine Niederlassung und wird
        niedriger bewertet.</p>
        <p>Angezeigt werden nur <strong>relevante</strong> Kandidaten (Ähnlichkeit ab 80 %); gibt es keine, steht dort
        „Keine relevante Dublette gefunden“. Über den Link darunter lassen sich alle Kandidaten einblenden.</p>
        <p><em>KI-Bewertung</em> lässt das LLM jeden angezeigten Kandidaten als <strong>Dublette</strong>, <strong>mögliche
        Dublette</strong>, <strong>verwandt</strong> (Niederlassung, Konzerngesellschaft, Ansprechpartner) oder
        <strong>verschieden</strong> einstufen – mit Begründung. Als verschieden bewertete Kandidaten werden danach
        ausgeblendet.</p>`
    },
    {
      title: "Dublettenübersicht für Geschäftspartner",
      html: `<p>Die <em>Dublettenübersicht</em> (Schaltfläche auf der Geschäftspartnersuche) führt die Dublettenprüfung für
        <strong>alle</strong> Partner aus – ohne KI – und zeigt die Partner, die wahrscheinlich mehrfach angelegt sind.
        Jedes Kandidatenpaar erhält die Stufe seines stärksten Hinweises:</p>
        <ul>
          <li><strong>Starker Schlüssel:</strong> gleiche Steuernummer, IBAN, Identifikations- oder externe Partnernummer,
          gleiches Geburtsdatum oder Kunde und Lieferant verweisen aufeinander</li>
          <li><strong>Gleiche Adresse:</strong> ähnlicher Name an derselben Adresse</li>
          <li><strong>Ähnlicher Name:</strong> nur der Name ist ähnlich (häufig auch Niederlassungen)</li>
          <li><strong>Nur semantisch:</strong> nur über die Embeddings ähnlich – viele Fehltreffer, daher standardmäßig aus</li>
        </ul>
        <p>Partner mit ähnlichem Namen werden zu <strong>Gruppen</strong> zusammengefasst (A ähnelt B, B ähnelt C → eine
        Gruppe A, B, C). Paare mit gemeinsamem Schlüssel, aber <em>abweichendem Namen</em> sind meist Datenfehler (z. B.
        kopierte Bankdaten) und erscheinen einzeln mit dem Hinweis <em>Schlüssel prüfen</em>.</p>
        <p>Ein Klick auf eine Partnernummer öffnet die Detailseite im Abschnitt <em>Dubletten</em>; dort lässt sich der
        Partner einzeln per <em>KI-Bewertung</em> prüfen. <em>CSV-Export</em> speichert die angezeigten Gruppen (eine Zeile je
        Partner), <em>Neu berechnen</em> ermittelt die Kandidaten neu.</p>`
    }
  ];

  return Controller.extend("fis.mdsa.controller.Help", {
    onInit() {
      this.getView().setModel(new JSONModel({ sections: SECTIONS }), "help");
    },

    onBack() {
      if (History.getInstance().getPreviousHash() !== undefined) {
        window.history.go(-1);
      } else {
        this.getOwnerComponent().getRouter().navTo("search", {}, true);
      }
    }
  });
});
