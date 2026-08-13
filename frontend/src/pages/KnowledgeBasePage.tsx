import { useEffect, useRef, useState } from "react";
import { type DocumentInfo, listDocuments, uploadDocument } from "../api/client";

const STATUS_LABEL: Record<DocumentInfo["status"], string> = {
  pending: "处理中",
  done: "已入库",
  error: "失败",
};

export function KnowledgeBasePage() {
  const [documents, setDocuments] = useState<DocumentInfo[]>([]);
  const [uploading, setUploading] = useState(false);
  const [uploadError, setUploadError] = useState<string | null>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);

  async function refresh() {
    try {
      setDocuments(await listDocuments());
    } catch {
      // transient network error while polling; next tick will retry
    }
  }

  useEffect(() => {
    refresh();
    const hasPending = () => documents.some((d) => d.status === "pending");
    const interval = setInterval(() => {
      if (hasPending() || documents.length === 0) refresh();
    }, 2000);
    return () => clearInterval(interval);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [documents.length]);

  async function handleFileChange(e: React.ChangeEvent<HTMLInputElement>) {
    const file = e.target.files?.[0];
    if (!file) return;

    setUploading(true);
    setUploadError(null);
    try {
      await uploadDocument(file);
      await refresh();
    } catch (err) {
      setUploadError((err as Error).message);
    } finally {
      setUploading(false);
      if (fileInputRef.current) fileInputRef.current.value = "";
    }
  }

  return (
    <div className="kb-page">
      <div className="kb-upload">
        <input
          ref={fileInputRef}
          type="file"
          accept=".pdf,.docx,.txt,.md"
          onChange={handleFileChange}
          disabled={uploading}
        />
        {uploading && <span>上传中…</span>}
        {uploadError && <span className="chat-error">{uploadError}</span>}
      </div>

      <table className="kb-table">
        <thead>
          <tr>
            <th>文件名</th>
            <th>状态</th>
            <th>上传时间</th>
          </tr>
        </thead>
        <tbody>
          {documents.map((doc) => (
            <tr key={doc.id}>
              <td>{doc.filename}</td>
              <td>
                <span className={`kb-status kb-status--${doc.status}`}>{STATUS_LABEL[doc.status]}</span>
                {doc.error && <div className="chat-error">{doc.error}</div>}
              </td>
              <td>{new Date(doc.created_at).toLocaleString()}</td>
            </tr>
          ))}
          {documents.length === 0 && (
            <tr>
              <td colSpan={3} className="kb-empty">
                还没有上传任何文档
              </td>
            </tr>
          )}
        </tbody>
      </table>
    </div>
  );
}
