import { Authenticated, Refine } from "@refinedev/core";
import routerProvider from "@refinedev/react-router-v6";
import { BrowserRouter, Navigate, Route, Routes } from "react-router-dom";
import { authProvider } from "./authProvider";
import { dataProvider } from "./dataProvider";
import { Layout } from "./components/Layout";
import { HotspotsPage } from "./pages/HotspotsPage";
import { KnowledgePage } from "./pages/KnowledgePage";
import { LoginPage } from "./pages/LoginPage";
import { OrgPage } from "./pages/OrgPage";

export default function App() {
  return (
    <BrowserRouter basename="/app">
      <Refine
        dataProvider={dataProvider}
        authProvider={authProvider}
        routerProvider={routerProvider}
        resources={[
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
            <Route path="/" element={<Navigate to="/hotspots" replace />} />
            <Route path="/hotspots" element={<HotspotsPage />} />
            <Route path="/knowledge" element={<KnowledgePage />} />
            <Route path="/org" element={<OrgPage />} />
          </Route>
          <Route path="*" element={<Navigate to="/hotspots" replace />} />
        </Routes>
      </Refine>
    </BrowserRouter>
  );
}
