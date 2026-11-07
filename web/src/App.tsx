import { Authenticated, Refine } from "@refinedev/core";
import routerProvider from "@refinedev/react-router-v6";
import { BrowserRouter, Navigate, Route, Routes } from "react-router-dom";
import { authProvider } from "./authProvider";
import { dataProvider } from "./dataProvider";
import { Layout } from "./components/Layout";
import { HomePage } from "./pages/HomePage";
import { HotspotsPage } from "./pages/HotspotsPage";
import { KnowledgePage } from "./pages/KnowledgePage";
import { LoginPage } from "./pages/LoginPage";
import { OrgPage } from "./pages/OrgPage";
import { WorksPage } from "./pages/WorksPage";

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
            <Route path="/works" element={<WorksPage />} />
            <Route path="/hotspots" element={<HotspotsPage />} />
            <Route path="/knowledge" element={<KnowledgePage />} />
            <Route path="/org" element={<OrgPage />} />
          </Route>
          <Route path="*" element={<Navigate to="/" replace />} />
        </Routes>
      </Refine>
    </BrowserRouter>
  );
}
