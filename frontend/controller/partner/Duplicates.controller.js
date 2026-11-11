sap.ui.define([
  "sap/ui/core/mvc/Controller",
  "sap/ui/core/routing/History",
  "sap/ui/model/json/JSONModel",
  "sap/m/MessageBox",
  "fis/mdsa/controller/api"
], (Controller, History, JSONModel, MessageBox, api) => {
  "use strict";

  const csvField = (value) => {
    const text = Array.isArray(value) ? value.join(", ") : String(value ?? "");
    return /[";\n]/.test(text) ? `"${text.replace(/"/g, '""')}"` : text;
  };

  // overview of all partners with probable duplicates: groups from the heuristic duplicate check (no LLM);
  // the LLM assessment runs per partner on its object page
  return Controller.extend("fis.mdsa.controller.partner.Duplicates", {
    onInit() {
      this.model = new JSONModel({ maxTier: "3", includeDeleted: false, sameNameOnly: false, filter: "",
        result: null, shown: [], busy: false, info: "" });
      this.getView().setModel(this.model, "o");
      this.getOwnerComponent().getRouter().getRoute("partnerDuplicates").attachPatternMatched(this.onRouteMatched, this);
    },

    onRouteMatched() {
      if (!this.model.getProperty("/result")) {   // keep the list (and its scroll position) when coming back
        this.load(false);
      }
    },

    async load(refresh) {
      const m = this.model;
      const params = new URLSearchParams({ max_tier: m.getProperty("/maxTier"),
        include_deleted: m.getProperty("/includeDeleted"), refresh });
      const request = (this.request || 0) + 1;
      this.request = request;
      m.setProperty("/busy", true);
      try {
        const result = await api(`api/partners/duplicates?${params}`);
        if (request === this.request) {
          m.setProperty("/result", result);
          this.applyFilter();
        }
      } catch (e) {
        MessageBox.error(`Dublettenübersicht konnte nicht geladen werden: ${e.message}`);
      } finally {
        if (request === this.request) {
          m.setProperty("/busy", false);
        }
      }
    },

    onTierChange() {
      this.load(false);
    },

    onRefresh() {
      this.load(true);
    },

    // client-side filter: text over partner number, name, address; optionally hide "same key, other name" pairs
    applyFilter() {
      const m = this.model;
      const result = m.getProperty("/result");
      if (!result) {
        return;
      }
      const words = (m.getProperty("/filter") || "").toLowerCase().split(/\s+/).filter(Boolean);
      const sameNameOnly = m.getProperty("/sameNameOnly");
      const shown = result.clusters.filter((c) => {
        if (sameNameOnly && c.name_differs) {
          return false;
        }
        if (!words.length) {
          return true;
        }
        const text = c.partners.map((p) => `${p.partner} ${p.name} ${p.address} ${p.kunnr} ${p.lifnr}`).join(" ").toLowerCase();
        return words.every((w) => text.includes(w));
      });
      m.setProperty("/shown", shown);
      const partners = shown.reduce((n, c) => n + c.size, 0);
      const time = result.computed_at ? ` · berechnet ${new Date(result.computed_at).toLocaleString("de-DE")}` : "";
      m.setProperty("/info", `${shown.length} Gruppen mit ${partners} Partnereinträgen (${result.pairs} Kandidatenpaare) `
        + `von ${result.total_partners} Geschäftspartnern${time} · ohne KI, Bewertung je Partner auf der Detailseite`);
    },

    onPartnerPress(event) {
      const partner = event.getSource().getBindingContext("o").getProperty("partner");
      this.getOwnerComponent().getRouter().navTo("partner", { partner, "?query": { section: "dubletten" } });
    },

    // golden record wizard for the group
    onMerge(event) {
      const partners = event.getSource().getBindingContext("o").getProperty("partners").map((p) => p.partner);
      this.getOwnerComponent().getRouter().navTo("partnerMerge", { partners: partners.join(",") });
    },

    // one line per partner and group (semicolon separated, opens directly in Excel)
    onExport() {
      const header = ["Gruppe", "Evidenz", "Ähnlichkeit", "Signale", "Abweichender Name", "Partner", "Name", "Partnertyp",
        "Rollen", "Kunde", "Lieferant", "Adresse", "Löschvormerkung"];
      const lines = [header];
      for (const c of this.model.getProperty("/shown")) {
        for (const p of c.partners) {
          lines.push([c.id, c.tier_text, c.score, c.signals, c.name_differs ? "X" : "", p.partner, p.name, p.type_text,
            p.roles, p.kunnr, p.lifnr, p.address, p.deleted ? "X" : ""]);
        }
      }
      const csv = "﻿" + lines.map((l) => l.map(csvField).join(";")).join("\r\n");
      const link = document.createElement("a");
      link.href = URL.createObjectURL(new Blob([csv], { type: "text/csv;charset=utf-8" }));
      link.download = `Dubletten_Geschaeftspartner_${new Date().toISOString().slice(0, 10)}.csv`;
      link.click();
      setTimeout(() => URL.revokeObjectURL(link.href), 1000);
    },

    onHelp() {
      this.getOwnerComponent().getRouter().navTo("help");
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
