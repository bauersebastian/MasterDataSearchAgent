sap.ui.define([
  "sap/ui/core/mvc/Controller",
  "sap/ui/model/json/JSONModel",
  "sap/m/MessageBox",
  "sap/m/Popover",
  "sap/m/List",
  "sap/m/StandardListItem",
  "fis/mdsa/controller/api"
], (Controller, JSONModel, MessageBox, Popover, List, StandardListItem, api) => {
  "use strict";

  const EXAMPLES = [
    { text: "Sommer & Winter", hint: "Hybrid: Name - mehrfach angelegt, siehe Dubletten" },
    { text: "somer wintr", hint: "Tippfehler werden toleriert" },
    { text: "Spedition", hint: "Text und Bedeutung: auch Logistiker ohne das Wort im Namen" },
    { text: "Lieferant für Kabelverschraubungen", hint: "Semantisch: über die gelieferten Materialien (Einkaufsinfosätze)" },
    { text: "BMW", hint: "Suchbegriff und Name" },
    { text: "info@fis-gmbh.de", hint: "Exakt: E-Mail-Adresse" },
    { text: "DE79100100100012345678", hint: "Exakt: IBAN" },
    { text: "300022", hint: "Exakt: Partnernummer" },
    { text: "Lieferant für Akkuschrauber in Unterfranken mit E-Mail-Adresse", hint: "KI-Suche: Rolle, Region und Kommunikation prüfen", mode: "ai" },
    { text: "Kunde in Österreich oder der Schweiz mit Umsatzangabe", hint: "KI-Suche: Land und Kundendaten prüfen", mode: "ai" }
  ];
  const MODE_TEXT = { hybrid: "Hybrid", semantic: "Semantisch", lexical: "Unscharf", ai: "KI-Suche" };

  return Controller.extend("fis.mdsa.controller.partner.Search", {
    onInit() {
      this.model = this.getOwnerComponent().getModel("bp");
      this.model.setProperty("/examples", EXAMPLES);
      this.getView().setModel(new JSONModel({ items: [] }), "suggest");
      this.getOwnerComponent().getRouter().getRoute("partnerSearch").attachPatternMatched(this.onRouteMatched, this);
    },

    // #partner?query=...&mode=... -> run the search (bookmarkable, browser back works)
    onRouteMatched(event) {
      this.byId("objectSwitch").setSelectedKey("partner");
      const args = event.getParameter("arguments")["?query"] || {};
      const query = args.query || "";
      const mode = args.mode || this.model.getProperty("/mode");
      const last = this.model.getProperty("/result");
      this.model.setProperty("/query", query);
      this.model.setProperty("/mode", mode);
      if (!query) {
        this.model.setProperty("/result", null);
      } else if (!last || last.query !== query || last.mode !== mode) {
        this.runSearch();
      }
    },

    navigate(query) {
      const mode = this.model.getProperty("/mode");
      this.getOwnerComponent().getRouter().navTo("partnerSearch", { query: query ? { query, mode } : {} });
    },

    // Geschäftspartner <-> Material: the material search comes back with its last query
    onObjectSwitch(event) {
      if (event.getParameter("item").getKey() !== "material") {
        return;
      }
      const m = this.getOwnerComponent().getModel();
      const query = m.getProperty("/result/query");
      this.getOwnerComponent().getRouter().navTo("search", { query: query ? { query, mode: m.getProperty("/mode") } : {} });
    },

    onSearch(event) {
      const item = event.getParameter("suggestionItem");
      if (item) {   // chosen suggestion -> open the partner directly
        this.getOwnerComponent().getRouter().navTo("partner", { partner: item.getKey() });
        return;
      }
      if (event.getParameter("clearButtonPressed")) {
        this.navigate("");
        return;
      }
      const query = (event.getParameter("query") || "").trim();
      if (!query) {
        return;
      }
      if (query === this.model.getProperty("/result/query")) {
        this.runSearch();   // same hash -> no route event
      } else {
        this.navigate(query);
      }
    },

    async onSuggest(event) {
      const field = event.getSource();
      const value = event.getParameter("suggestValue").trim();
      if (value.length < 2 || this.model.getProperty("/mode") === "ai") {   // AI requests are sentences, not names
        this.getView().getModel("suggest").setProperty("/items", []);
        return;
      }
      try {
        const items = await api(`api/partners/suggest?q=${encodeURIComponent(value)}`);
        if (field.getValue().trim() === value) {   // ignore answers to outdated keystrokes
          this.getView().getModel("suggest").setProperty("/items", items);
          field.suggest();
        }
      } catch (e) {
        // suggestions are optional
      }
    },

    onModeChange() {
      const query = this.model.getProperty("/query").trim();
      if (query) {
        this.navigate(query);
      }
    },

    onFilterChange() {
      if (this.model.getProperty("/result")) {
        this.runSearch();
      }
    },

    onExample(event) {
      const example = event.getSource().getBindingContext("bp").getObject();
      if (example.mode && this.getOwnerComponent().getModel().getProperty("/config/openai_configured")) {
        this.model.setProperty("/mode", example.mode);
      }
      this.navigate(example.text);
    },

    onDidYouMean() {
      this.navigate(this.model.getProperty("/result/did_you_mean"));
    },

    onReset() {
      ["/type", "/role", "/country", "/group"].forEach((p) => this.model.setProperty(p, []));
      this.model.setProperty("/includeDeleted", false);
      this.model.setProperty("/mode", "hybrid");
      this.navigate("");
    },

    async runSearch() {
      const m = this.model;
      const body = {
        query: m.getProperty("/query").trim(), mode: m.getProperty("/mode"),
        type: m.getProperty("/type"), role: m.getProperty("/role"), country: m.getProperty("/country"),
        group: m.getProperty("/group"), include_deleted: m.getProperty("/includeDeleted"), limit: 200
      };
      if (!body.query) {
        return;
      }
      m.setProperty("/busy", true);
      if (!m.getProperty("/result")) {
        m.setProperty("/result", { query: body.query, mode: body.mode, hits: [], warnings: [], expansions: [] });
      }
      const started = performance.now();
      try {
        const result = await api("api/partners/search", body);
        m.setProperty("/result", result);
        m.setProperty("/aiInfo", result.interpretation ? this.formatInterpretation(result) : "");
        const seconds = ((performance.now() - started) / 1000).toFixed(2);
        const shown = result.total > result.hits.length ? `, die besten ${result.hits.length} angezeigt` : "";
        m.setProperty("/resultInfo", `${MODE_TEXT[result.mode]} · ${seconds} s${shown}`);
        if (result.mode !== body.mode) {
          m.setProperty("/mode", result.mode);
        }
      } catch (e) {
        MessageBox.error(`Suche fehlgeschlagen: ${e.message}`);
      } finally {
        m.setProperty("/busy", false);
      }
    },

    // "Verstanden als: ..." line of the AI search
    formatInterpretation(result) {
      const i = result.interpretation;
      const requirements = i.requirements.length ? ` · Anforderungen: ${i.requirements.map((r) => r.text).join("; ")}` : "";
      return `Verstanden als: ${i.product} · Suchbegriffe: ${i.search_terms}${requirements} · `
        + `${result.candidates_checked} Kandidaten anhand der Stammdaten geprüft`;
    },

    onDuplicateOverview() {
      this.getOwnerComponent().getRouter().navTo("partnerDuplicates");
    },

    onHelp() {
      this.getOwnerComponent().getRouter().navTo("help");
    },

    onHitPress(event) {
      const partner = event.getSource().getBindingContext("bp").getProperty("partner");
      this.getOwnerComponent().getRouter().navTo("partner", { partner });
    },

    // popover: how each query word was expanded (exact, prefix, compound part, typo)
    onShowExpansions(event) {
      const kinds = { exact: "exakt", prefix: "Präfix", compound: "Wortteil", fuzzy: "Tippfehler" };
      const items = this.model.getProperty("/result/expansions").map((e) => new StandardListItem({
        title: e.token,
        description: e.terms.length ? e.terms.map((t) => `${t.term} (${kinds[t.kind]})`).join(", ") : "kein Treffer im Text",
        wrapping: true
      }));
      if (this.popover) {
        this.popover.destroy();
      }
      this.popover = new Popover({
        title: "Unscharfe Textsuche: Suchwörter", placement: "Bottom", contentWidth: "28rem",
        content: [new List({ items })]
      });
      this.getView().addDependent(this.popover);
      this.popover.openBy(event.getSource());
    }
  });
});
