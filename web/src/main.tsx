import React from "react";
import ReactDOM from "react-dom/client";
import App from "./App";
import "./styles.css";
import "./floor.css";
import "./wuxing.css";
import "./knowledge.css";
import { initWuxing } from "./wuxing";

// 首屏渲染前就算出今日宜用之行，避免主题闪烁
initWuxing();

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
);
