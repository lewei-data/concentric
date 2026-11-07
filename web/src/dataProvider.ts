import type {
  BaseRecord,
  DataProvider,
  GetListParams,
  GetListResponse,
  GetOneParams,
  GetOneResponse,
} from "@refinedev/core";

const API = "/api";

interface ListResponse<T> {
  ok: boolean;
  total: number;
  page: number;
  size: number;
  items: T[];
}

async function requestJson<T>(url: string, init?: RequestInit): Promise<T> {
  const res = await fetch(url, { credentials: "include", ...init });
  if (res.status === 401) {
    // 会话过期时回到新版登录页，登录后仍留在新版界面
    window.location.assign("/app/login");
    throw new Error("unauthorized");
  }
  if (!res.ok) {
    throw new Error(`HTTP ${res.status}`);
  }
  return (await res.json()) as T;
}

function extractQ(filters: unknown): string {
  if (!Array.isArray(filters)) return "";
  for (const filter of filters) {
    if (!filter || typeof filter !== "object") continue;
    const item = filter as { field?: string; value?: unknown };
    if (item.field === "q") {
      if (Array.isArray(item.value)) return String(item.value[0] ?? "");
      return String(item.value ?? "");
    }
  }
  return "";
}

async function getList<TData extends BaseRecord = BaseRecord>({
  resource,
  pagination,
  filters,
}: GetListParams): Promise<GetListResponse<TData>> {
  const current = pagination?.current ?? 1;
  const pageSize = pagination?.pageSize ?? 20;
  const q = extractQ(filters);
  const params = new URLSearchParams({
    page: String(current),
    size: String(pageSize),
  });
  if (q) params.set("q", q);
  const json = await requestJson<ListResponse<Record<string, unknown>>>(
    `${API}/${resource}?${params.toString()}`,
  );
  return {
    data: (json.items ?? []) as unknown as TData[],
    total: json.total ?? 0,
  };
}

async function getOne<TData extends BaseRecord = BaseRecord>({
  resource,
  id,
}: GetOneParams): Promise<GetOneResponse<TData>> {
  const data = await requestJson<Record<string, unknown>>(
    `${API}/${resource}/${encodeURIComponent(String(id))}`,
  );
  return { data: data as unknown as TData };
}

export const dataProvider: DataProvider = {
  getApiUrl: () => API,
  getList,
  getOne,
  // P2 再补：热点/知识的写接口目前不存在，先统一拒绝
  create: () => Promise.reject(new Error("not implemented")),
  update: () => Promise.reject(new Error("not implemented")),
  deleteOne: () => Promise.reject(new Error("not implemented")),
};
