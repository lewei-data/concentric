import { Suspense, lazy } from "react";
import { Authenticated, Refine } from "@refinedev/core";
import routerProvider from "@refinedev/react-router-v6";
import { BrowserRouter, Navigate, Route, Routes } from "react-router-dom";
import { authProvider } from "./authProvider";
import { dataProvider } from "./dataProvider";
import { Layout } from "./components/Layout";
import { HomePage } from "./pages/HomePage";
import { LoginPage } from "./pages/LoginPage";

// 路由级代码分割：非首屏页面按需加载，主包只保留首页 + 登录页
const HotspotsPage = lazy(() => import("./pages/HotspotsPage").then((m) => ({ default: m.HotspotsPage })));
const InspirationPage = lazy(() => import("./pages/InspirationPage").then((m) => ({ default: m.InspirationPage })));
const KnowledgePage = lazy(() => import("./pages/KnowledgePage").then((m) => ({ default: m.KnowledgePage })));
const MessagesPage = lazy(() => import("./pages/MessagesPage").then((m) => ({ default: m.MessagesPage })));
const OpsPage = lazy(() => import("./pages/OpsPage").then((m) => ({ default: m.OpsPage })));
const OrgPage = lazy(() => import("./pages/OrgPage").then((m) => ({ default: m.OrgPage })));
const AccountPage = lazy(() => import("./pages/AccountPage").then((m) => ({ default: m.AccountPage })));
const ScenePage = lazy(() => import("./pages/ScenePage").then((m) => ({ default: m.ScenePage })));
const WorksPage = lazy(() => import("./pages/WorksPage").then((m) => ({ default: m.WorksPage })));
const FactoryPage = lazy(() => import("./pages/FactoryPage").then((m) => ({ default: m.FactoryPage })));

// 懒加载路由的过渡画面：只占 outlet 区域，顶部导航保持不动
function PageFallback() {
  return (
    <div style={{ display: "grid", placeItems: "center", minHeight: "40vh", color: "#64748b", fontSize: 14 }}>
      加载中…
    </div>
  );
}

export default function App() {
  return (
    <BrowserRouter basename="/app">
      <Refine
        dataProvider={dataProvider}
        authProvider={authProvider}
        routerProvider={routerProvider}
        resources={[
          { name: "works", list: "/works" },
          { name: "hotspots", list: "/hotspots" },
          { name: "knowledge", list: "/knowledge" },
          { name: "org", list: "/org" },
        ]}
      >
        <Routes>
          <Route path="/login" element={<LoginPage />} />
          <Route
            element={
              <Authenticated key="app-auth" fallback={<Navigate to="/login" replace />}>
                <Layout />
              </Authenticated>
            }
          >
            <Route path="/" element={<HomePage />} />
            <Route path="/inspiration" element={<Suspense fallback={<PageFallback />}><InspirationPage /></Suspense>} />
            <Route path="/ops" element={<Suspense fallback={<PageFallback />}><OpsPage /></Suspense>} />
            <Route path="/works" element={<Suspense fallback={<PageFallback />}><WorksPage /></Suspense>} />
            <Route path="/factory" element={<Suspense fallback={<PageFallback />}><FactoryPage /></Suspense>} />
            <Route path="/hotspots" element={<Suspense fallback={<PageFallback />}><HotspotsPage /></Suspense>} />
            <Route path="/knowledge" element={<Suspense fallback={<PageFallback />}><KnowledgePage /></Suspense>} />
            <Route path="/messages" element={<Suspense fallback={<PageFallback />}><MessagesPage /></Suspense>} />
            <Route path="/org" element={<Suspense fallback={<PageFallback />}><OrgPage /></Suspense>} />
            <Route path="/account" element={<Suspense fallback={<PageFallback />}><AccountPage /></Suspense>} />
            <Route path="/account/assets" element={<Suspense fallback={<PageFallback />}><AccountPage /></Suspense>} />
            <Route path="/account/settings" element={<Suspense fallback={<PageFallback />}><AccountPage /></Suspense>} />
            <Route path="/account/help" element={<Suspense fallback={<PageFallback />}><AccountPage /></Suspense>} />
            <Route path="/office" element={<Suspense fallback={<PageFallback />}><ScenePage scene="office" /></Suspense>} />
            <Route path="/meeting" element={<Suspense fallback={<PageFallback />}><ScenePage scene="meeting" /></Suspense>} />
          </Route>
          <Route path="*" element={<Navigate to="/" replace />} />
        </Routes>
      </Refine>
    </BrowserRouter>
  );
}
