import { useCallback, useEffect, useRef, useState } from "react";

import { encodeImage, type EncodeFailure } from "@/lib/imageEncode";

export type AttachmentKind = "image" | "file";
export type AttachmentStatus = "encoding" | "ready" | "error";
export type AttachmentError =
  | "unsupported_type"
  | "too_many_files"
  | "magic_mismatch"
  | "decode_failed"
  | "too_large"
  | "io";

export interface AttachedFile {
  id: string;
  file: File;
  kind: AttachmentKind;
  previewUrl: string;
  status: AttachmentStatus;
  dataUrl?: string;
  encodedBytes?: number;
  normalized?: boolean;
  error?: AttachmentError;
}

export interface RestoredReadyFile {
  dataUrl: string;
  name?: string;
  kind?: AttachmentKind;
}

export const MAX_FILES_PER_MESSAGE = 10;
export const MAX_DOCUMENT_BYTES = 20 * 1024 * 1024;

const IMAGE_MIMES = new Set(["image/png", "image/jpeg", "image/webp", "image/gif"]);
const DOCUMENT_MIMES = new Set([
  "application/pdf",
  "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
  "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
  "application/vnd.openxmlformats-officedocument.presentationml.presentation",
  "text/plain", "text/markdown", "text/csv", "text/html", "text/xml", "application/json",
  "application/xml", "application/javascript", "text/javascript",
]);
const DOCUMENT_SUFFIXES = new Set([
  ".pdf", ".docx", ".xlsx", ".pptx", ".txt", ".md", ".csv", ".json", ".xml", ".html",
  ".htm", ".log", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".py", ".js", ".jsx",
  ".ts", ".tsx", ".css", ".scss", ".sh", ".sql", ".java", ".go", ".rs", ".c", ".h",
  ".cpp", ".hpp", ".rb", ".php", ".swift", ".kt", ".kts", ".cs", ".vue", ".svelte",
]);

function uuid(): string {
  return crypto.randomUUID?.() ?? `attachment-${Date.now()}-${Math.random().toString(36).slice(2)}`;
}

function suffix(name: string): string {
  const index = name.lastIndexOf(".");
  return index >= 0 ? name.slice(index).toLowerCase() : "";
}

function kindForFile(file: File): AttachmentKind | null {
  if (IMAGE_MIMES.has(file.type)) return "image";
  return DOCUMENT_MIMES.has(file.type) || DOCUMENT_SUFFIXES.has(suffix(file.name)) ? "file" : null;
}

function dataUrlMime(dataUrl: string): string {
  return /^data:([^;,]+)[;,]/.exec(dataUrl)?.[1] || "application/octet-stream";
}

function fileFromDataUrl(dataUrl: string, name?: string): File {
  const mime = dataUrlMime(dataUrl);
  try {
    const binary = atob(dataUrl.split(",", 2)[1] ?? "");
    const bytes = Uint8Array.from(binary, (char) => char.charCodeAt(0));
    return new File([bytes], name || `attachment${suffix(name || "")}`, { type: mime });
  } catch {
    return new File([], name || "attachment", { type: mime });
  }
}

function mapEncodeFailure(reason: EncodeFailure["reason"]): AttachmentError {
  if (reason === "invalid_mime" || reason === "magic_mismatch") return "magic_mismatch";
  if (reason === "too_large_after_normalize") return "too_large";
  if (reason === "io") return "io";
  return "decode_failed";
}

function readAsDataUrl(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onerror = () => reject(reader.error ?? new Error("file read failed"));
    reader.onload = () => typeof reader.result === "string"
      ? resolve(reader.result)
      : reject(new Error("file read returned no data"));
    reader.readAsDataURL(file);
  });
}

/** Stages visual and document/source attachments for one chat turn. */
export function useAttachedFiles() {
  const [files, setFiles] = useState<AttachedFile[]>([]);
  const filesRef = useRef<AttachedFile[]>([]);
  filesRef.current = files;

  const setEntry = useCallback((id: string, patch: Partial<AttachedFile>) => {
    setFiles((prev) => {
      const next = prev.map((entry) => entry.id === id ? { ...entry, ...patch } : entry);
      filesRef.current = next;
      return next;
    });
  }, []);

  const enqueue = useCallback((incoming: Iterable<File>) => {
    const rejected: Array<{ file: File; reason: AttachmentError }> = [];
    const toAdd: AttachedFile[] = [];
    let slots = MAX_FILES_PER_MESSAGE - filesRef.current.length;
    for (const file of incoming) {
      const kind = kindForFile(file);
      if (!kind) {
        rejected.push({ file, reason: "unsupported_type" });
      } else if (kind === "file" && file.size > MAX_DOCUMENT_BYTES) {
        rejected.push({ file, reason: "too_large" });
      } else if (slots <= 0) {
        rejected.push({ file, reason: "too_many_files" });
      } else {
        slots -= 1;
        toAdd.push({
          id: uuid(), file, kind, status: "encoding",
          previewUrl: kind === "image" ? URL.createObjectURL(file) : "",
        });
      }
    }
    if (toAdd.length > 0) {
      const next = [...filesRef.current, ...toAdd];
      filesRef.current = next;
      setFiles(next);
      for (const entry of toAdd) {
        const result = entry.kind === "image" ? encodeImage(entry.file) : readAsDataUrl(entry.file);
        void result.then(
          (value) => {
            if (typeof value === "string") {
              setEntry(entry.id, { status: "ready", dataUrl: value, encodedBytes: entry.file.size });
            } else if (value.ok) {
              setEntry(entry.id, {
                status: "ready", dataUrl: value.dataUrl, encodedBytes: value.bytes, normalized: value.normalized,
              });
            } else {
              setEntry(entry.id, { status: "error", error: mapEncodeFailure(value.reason) });
            }
          },
          () => setEntry(entry.id, { status: "error", error: entry.kind === "file" ? "io" : "decode_failed" }),
        );
      }
    }
    return { rejected };
  }, [setEntry]);

  const remove = useCallback((id: string) => {
    let nextFocusId: string | null = null;
    setFiles((prev) => {
      const index = prev.findIndex((entry) => entry.id === id);
      if (index < 0) return prev;
      const target = prev[index];
      if (target.previewUrl) URL.revokeObjectURL(target.previewUrl);
      const next = [...prev.slice(0, index), ...prev.slice(index + 1)];
      filesRef.current = next;
      nextFocusId = (next[index] ?? next[index - 1])?.id ?? null;
      return next;
    });
    return { nextFocusId };
  }, []);

  const clear = useCallback(() => {
    setFiles((prev) => {
      prev.forEach((entry) => entry.previewUrl && URL.revokeObjectURL(entry.previewUrl));
      filesRef.current = [];
      return [];
    });
  }, []);

  const restoreReadyFiles = useCallback((restored: RestoredReadyFile[]) => {
    const next = restored.slice(0, MAX_FILES_PER_MESSAGE).map((item): AttachedFile => {
      const file = fileFromDataUrl(item.dataUrl, item.name);
      const kind = item.kind ?? (IMAGE_MIMES.has(dataUrlMime(item.dataUrl)) ? "image" : "file");
      return { id: uuid(), file, kind, status: "ready", dataUrl: item.dataUrl, encodedBytes: file.size,
        previewUrl: kind === "image" ? item.dataUrl : "" };
    });
    setFiles((prev) => {
      prev.forEach((entry) => entry.previewUrl && URL.revokeObjectURL(entry.previewUrl));
      filesRef.current = next;
      return next;
    });
  }, []);

  useEffect(() => () => {
    filesRef.current.forEach((entry) => entry.previewUrl && URL.revokeObjectURL(entry.previewUrl));
  }, []);

  return {
    files,
    enqueue,
    remove,
    clear,
    restoreReadyFiles,
    encoding: files.some((entry) => entry.status === "encoding"),
    full: files.length >= MAX_FILES_PER_MESSAGE,
  };
}
