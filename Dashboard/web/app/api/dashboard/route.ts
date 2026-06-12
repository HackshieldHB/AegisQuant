import { NextResponse } from "next/server";

export const dynamic = "force-dynamic";
export const revalidate = 0;

export async function GET() {
  const url = process.env.AEGIS_API_URL;
  const token = process.env.AEGIS_API_TOKEN;
  if (!url || !token) {
    return NextResponse.json({ error: "dashboard_api_not_configured" }, { status: 500 });
  }

  let response: Response;
  try {
    response = await fetch(url, {
      headers: { "X-Aegis-Token": token },
      cache: "no-store"
    });
  } catch (error) {
    const message = error instanceof Error ? error.message : "Backend API unreachable";
    return NextResponse.json({ error: "backend_unreachable", message }, { status: 502 });
  }

  const body = await response.text();
  return new NextResponse(body, {
    status: response.status,
    headers: {
      "Content-Type": response.headers.get("Content-Type") ?? "application/json",
      "Cache-Control": "no-store"
    }
  });
}
