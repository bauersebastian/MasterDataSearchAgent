sap.ui.define(["sap/ui/core/UIComponent", "sap/ui/model/json/JSONModel"], (UIComponent, JSONModel) => {
  "use strict";
  return UIComponent.extend("fis.mdsa.Component", {
    metadata: { manifest: "json", interfaces: ["sap.ui.core.IAsyncContentCreation"] },

    init() {
      UIComponent.prototype.init.apply(this, arguments);
      // search state lives in the component, so it survives the navigation to a material and back
      this.setModel(new JSONModel({
        config: { loaded: false }, query: "", mode: "hybrid", mtart: [], matkl: [], vendor: [], includeDeleted: false,
        result: null, busy: false
      }));
      fetch("api/config")
        .then((r) => (r.ok ? r.json() : Promise.reject(new Error(`${r.status} ${r.statusText}`))))
        .then((config) => this.getModel().setProperty("/config", { ...config, loaded: true }))
        .catch((e) => this.getModel().setProperty("/config", { loaded: true, error: e.message }));
      this.getRouter().initialize();
    }
  });
});
