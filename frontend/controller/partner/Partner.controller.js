sap.ui.define([
  "sap/ui/core/mvc/Controller",
  "sap/ui/core/routing/History",
  "sap/ui/model/json/JSONModel",
  "sap/m/MessageBox",
  "sap/m/Label",
  "sap/m/Text",
  "sap/m/Panel",
  "sap/m/Table",
  "sap/m/Column",
  "sap/m/ColumnListItem",
  "sap/m/ScrollContainer",
  "fis/mdsa/controller/api"
], (Controller, History, JSONModel, MessageBox, Label, Text, Panel, Table, Column, ColumnListItem, ScrollContainer, api) => {
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
    onInit() {
      this.getView().setModel(new JSONModel({ busy: true }), "p");
      this.getOwnerComponent().getRouter().getRoute("partner").attachPatternMatched(this.onRouteMatched, this);
      api("api/partners/tables").then((labels) => { this.tableLabels = labels; }).catch(() => { this.tableLabels = {}; });
    },

    async onRouteMatched(event) {
      const partner = event.getParameter("arguments").partner;
      const p = this.getView().getModel("p");
      p.setData({ partner, busy: true });
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
      }
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
