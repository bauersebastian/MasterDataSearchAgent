sap.ui.define([
  "sap/ui/core/mvc/Controller",
  "sap/ui/core/routing/History",
  "sap/ui/model/json/JSONModel",
  "sap/m/MessageBox",
  "sap/m/MessageToast",
  "sap/m/Label",
  "sap/m/Text",
  "sap/m/Panel",
  "sap/m/Table",
  "sap/m/Column",
  "sap/m/ColumnListItem",
  "sap/m/ScrollContainer",
  "fis/mdsa/controller/api",
  "fis/mdsa/controller/SapDialog"
], (Controller, History, JSONModel, MessageBox, MessageToast, Label, Text, Panel, Table, Column, ColumnListItem, ScrollContainer, api, SapDialog) => {
  "use strict";

  // German labels of the G000 fields (the rest is shown with its technical name)
  const G000_LABELS = {
    PARTNER: "Geschäftspartner", TYPE: "Partnertyp", BPKIND: "Partnerart", BU_GROUP: "Gruppierung",
    BPEXT: "Externe Partnernummer", BU_SORT1: "Suchbegriff 1", BU_SORT2: "Suchbegriff 2", TITLE: "Anrede",
    XDELE: "Archivierungsvormerkung", XBLCK: "Zentrale Sperre", NAME_ORG1: "Name 1", NAME_ORG2: "Name 2",
    NAME_ORG3: "Name 3", NAME_ORG4: "Name 4", LEGAL_ENTY: "Rechtsform", LEGAL_ORG: "Rechtsträger",
    FOUND_DAT: "Gründungsdatum", LIQUID_DAT: "Liquidationsdatum", NAME_LAST: "Nachname", NAME_FIRST: "Vorname",
    NAME_LAST2: "Geburtsname", NAMEMIDDLE: "Zweiter Vorname", TITLE_ACA1: "Akad. Titel", NAME1_TEXT: "Vollständiger Name",
    NICKNAME: "Rufname", INITIALS: "Initialen", LANGU_CORR: "Korrespondenzsprache", BU_LANGU: "Sprache",
    XSEXM: "Männlich", XSEXF: "Weiblich", XSEXU: "Geschlecht unbekannt", BIRTHDT: "Geburtsdatum", NATIO: "Nationalität",
    NAMCOUNTRY: "Land für Namensformat", NATPERS: "Natürliche Person", NAME_GRP1: "Gruppenname 1", NAME_GRP2: "Gruppenname 2",
    CONTACT: "Kontaktsperre", LOCATION_1: "Standort 1", LOCATION_2: "Standort 2", LOCATION_3: "Prüfziffer", AUGRP: "Berechtigungsgruppe"
  };
  const ICONS = { 1: "sap-icon://person-placeholder", 2: "sap-icon://building", 3: "sap-icon://group" };

  const isArray = (v) => Array.isArray(v);

  return Controller.extend("fis.mdsa.controller.partner.Partner", {
    ...SapDialog,

    onInit() {
      this.getView().setModel(new JSONModel({ busy: true }), "p");
      this.getView().setModel(new JSONModel({ candidates: null, shown: null, showAll: false, busy: false }), "d");
      this.getOwnerComponent().getRouter().getRoute("partner").attachPatternMatched(this.onRouteMatched, this);
      api("api/partners/tables").then((labels) => { this.tableLabels = labels; }).catch(() => { this.tableLabels = {}; });
    },

    async onRouteMatched(event) {
      const args = event.getParameter("arguments");
      const partner = args.partner;
      this.showDuplicates = (args["?query"] || {}).section === "dubletten";   // from the duplicate overview
      const p = this.getView().getModel("p");
      p.setData({ partner, busy: true });
      this.getView().getModel("d").setData({ candidates: null, shown: null, showAll: false, busy: true, assessment: null });
      this.byId("page").setSelectedSection(null);
      this.partner = partner;
      try {
        const data = await api(`api/partners/${encodeURIComponent(partner)}`);
        if (partner !== this.partner) {
          return;
        }
        p.setData({ ...data, icon: ICONS[data.type] || ICONS[2], graph: this.buildGraph(data), busy: false });
        this.buildGeneralForm(data);
        this.buildRawTables(data.tables);
      } catch (e) {
        p.setProperty("/busy", false);
        MessageBox.error(`Geschäftspartner ${partner} konnte nicht geladen werden: ${e.message}`);
        return;
      }
      this.loadCandidates(partner);
    },

    // --- duplicate check ---
    async loadCandidates(partner) {
      const d = this.getView().getModel("d");
      try {
        const result = await api(`api/partners/${encodeURIComponent(partner)}/duplicates`);
        if (partner === this.partner) {
          d.setData({ ...result, showAll: false, busy: false, assessment: null });
          this.updateShownCandidates();
          if (this.showDuplicates) {
            setTimeout(() => this.onScrollToDuplicates(), 0);
          }
        }
      } catch (e) {
        d.setProperty("/busy", false);
        MessageToast.show(`Dublettenkandidaten konnten nicht ermittelt werden: ${e.message}`);
      }
    },

    async onAssess() {
      const d = this.getView().getModel("d");
      const partner = this.partner;
      const candidates = d.getProperty("/shown").map((c) => c.partner);
      d.setProperty("/busy", true);
      try {
        const assessment = await api(`api/partners/${encodeURIComponent(partner)}/duplicates/assess`, { candidates });
        if (partner !== this.partner) {
          return;
        }
        const verdicts = Object.fromEntries(assessment.verdicts.map((v) => [v.partner, v]));
        d.setProperty("/candidates", d.getProperty("/candidates").map((c) => ({ ...c, verdict: verdicts[c.partner] || null })));
        d.setProperty("/assessment", assessment);
        d.setProperty("/duplicateFound", assessment.verdicts.some((v) => v.verdict === "Dublette" || v.verdict === "Mögliche Dublette"));
        this.updateShownCandidates();
      } catch (e) {
        MessageBox.error(`KI-Bewertung fehlgeschlagen: ${e.message}`);
      } finally {
        d.setProperty("/busy", false);
      }
    },

    // by default only relevant candidates (high similarity or strong shared key) that the LLM did not rate as unrelated
    updateShownCandidates() {
      const d = this.getView().getModel("d");
      const candidates = d.getProperty("/candidates") || [];
      const relevant = candidates.filter((c) => c.relevant && c.verdict?.verdict !== "Verschieden");
      d.setProperty("/relevantCount", relevant.length);
      d.setProperty("/shown", d.getProperty("/showAll") ? candidates : relevant);
    },

    onToggleAllCandidates() {
      const d = this.getView().getModel("d");
      d.setProperty("/showAll", !d.getProperty("/showAll"));
      this.updateShownCandidates();
    },

    // --- send the partner to SAP (shared dialog, see controller/SapDialog.js) ---
    onOpenSapDialog() {
      this.openSapDialog({ path: `api/partners/${encodeURIComponent(this.partner)}/xml`, object: "partner",
        label: `Geschäftspartner ${this.partner}`, subject: "Der Geschäftspartner", file: `partner_${this.partner}` });
    },

    onScrollToDuplicates() {
      this.byId("page").scrollToSection(this.byId("duplicatesSection").getId());
    },

    onCandidatePress(event) {
      this.openPartner(event.getSource().getBindingContext("d").getProperty("partner"));
    },

    // --- formatters ---
    phoneLines(phones) {
      return isArray(phones) ? phones.map((t) => t.number + (t.mobile ? " (mobil)" : "")).join("\n") : "";
    },

    lines(...lists) {
      return lists.filter(isArray).flat().join("\n");
    },

    relationPhones(phones, faxes) {
      return [...(phones || []), ...(faxes || []).map((f) => `${f} (Fax)`)].join("\n");
    },

    functionCodes(functionCode, department, departmentCode) {
      const parts = [];
      if (functionCode) {
        parts.push(`Funktion ${functionCode}`);
      }
      if (department || departmentCode) {
        parts.push(department || `Abteilung ${departmentCode}`);
      }
      return parts.join(" · ");
    },

    // --- general data form (filled G000 fields) ---
    buildGeneralForm(data) {
      const form = this.byId("generalForm");
      form.destroyContent();
      const values = { ...data.general, TYPE: `${data.general.TYPE} ${data.type_text}` };
      if (data.title) {
        values.TITLE = `${data.general.TITLE} ${data.title}`;
      }
      Object.entries(values).forEach(([field, value]) => {
        form.addContent(new Label({ text: G000_LABELS[field] || field, tooltip: field }));
        form.addContent(new Text({ text: value }));
      });
    },

    // --- relationship graph: the partner in the middle, organisations left, contact persons / others right ---
    buildGraph(data) {
      const nodes = [{ key: data.partner, title: data.name, description: data.type_text, status: "current", icon: ICONS[data.type] }];
      const lines = [];
      const seen = new Set([data.partner]);
      data.relations.forEach((r) => {
        if (!seen.has(r.partner)) {
          seen.add(r.partner);
          const status = !r.active ? "inactive" : r.reltyp !== "BUR001" ? "other" : r.type === "1" ? "contact" : "company";
          nodes.push({ key: r.partner, title: r.name || r.partner, description: [r.type_text, r.function].filter(Boolean).join(" · "),
            status, icon: ICONS[r.type] || ICONS[2] });
        }
        // organisation -> contact person; other relations in the direction of the extract
        const orgFirst = r.reltyp === "BUR001" ? r.type === "1" : r.direction === "F";
        const [from, to] = orgFirst ? [data.partner, r.partner] : [r.partner, data.partner];
        if (!lines.some((l) => l.from === from && l.to === to && l.reltyp === r.reltyp)) {
          lines.push({ from, to, reltyp: r.reltyp, title: r.reltyp === "BUR001" ? "Ansprechpartner" : r.label,
            description: [r.function, r.department, `${r.valid_from} - ${r.valid_to}`].filter(Boolean).join(" · ") });
        }
      });
      return { nodes, lines };
    },

    // --- all tables of the extract: one panel per table, single rows as field / value list ---
    buildRawTables(tables) {
      const box = this.byId("rawTables");
      box.destroyItems();
      const labels = this.tableLabels || {};
      tables.forEach(({ table, rows }) => {
        const fields = Object.keys(rows[0] || {}).filter((f) => f !== "PARTNER" && rows.some((r) => r[f]));
        const model = new JSONModel({ rows });
        let content;
        if (rows.length === 1) {
          content = new Table({ columns: [new Column({ header: new Text({ text: "Feld" }), width: "12rem" }),
            new Column({ header: new Text({ text: "Wert" }) })] });
          fields.forEach((f) => content.addItem(new ColumnListItem({ cells: [new Text({ text: f }), new Text({ text: rows[0][f] })] })));
        } else {
          content = new Table({
            fixedLayout: false,
            columns: fields.map((f) => new Column({ header: new Text({ text: f }) })),
            items: { path: "/rows", template: new ColumnListItem({ cells: fields.map((f) => new Text({ text: `{${f}}`, wrapping: false })) }) }
          });
          content.setModel(model);
          content = new ScrollContainer({ horizontal: true, vertical: false, width: "100%", content: [content] });
        }
        box.addItem(new Panel({
          headerText: `${table} – ${labels[table] || ""} (${rows.length} ${rows.length === 1 ? "Zeile" : "Zeilen"})`,
          expandable: true, expanded: table === "G000" ? false : rows.length <= 5, content: [content]
        }).addStyleClass("sapUiTinyMarginBottom"));
      });
    },

    // --- navigation ---
    onRelationPress(event) {
      this.openPartner(event.getSource().getBindingContext("p").getProperty("partner"));
    },

    onGraphNodeOpen(event) {
      this.openPartner(event.getSource().getParent().getKey());
    },

    openPartner(partner) {
      if (partner && partner !== this.partner) {
        this.getOwnerComponent().getRouter().navTo("partner", { partner });
      }
    },

    onMaterialPress(event) {
      const matnr = event.getSource().getBindingContext("p").getProperty("matnr");
      this.getOwnerComponent().getRouter().navTo("material", { matnr });
    },

    onBack() {
      if (History.getInstance().getPreviousHash() !== undefined) {
        window.history.go(-1);
      } else {
        this.getOwnerComponent().getRouter().navTo("partnerSearch", {}, true);
      }
    }
  });
});
