"use client";

import { use } from "react";
import SessionsShell from "@/components/sessions/SessionsShell";

export default function SessionDetailPage({ params }: PageProps<"/sessions/[id]">) {
  const { id } = use(params);
  return <SessionsShell selectedId={id} />;
}
