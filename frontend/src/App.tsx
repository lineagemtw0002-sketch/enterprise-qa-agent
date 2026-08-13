import { useState } from "react";
import { NavLink, Route, Routes } from "react-router-dom";
import "./App.css";
import { ChatPage, type ChatMessage } from "./pages/ChatPage";
import { KnowledgeBasePage } from "./pages/KnowledgeBasePage";

function App() {
  const [messages, setMessages] = useState<ChatMessage[]>([]);

  return (
    <div className="app">
      <header className="app-header">
        <span className="app-title">企业智能问答系统</span>
        <nav>
          <NavLink to="/" end className={({ isActive }) => (isActive ? "active" : "")}>
            对话
          </NavLink>
          <NavLink to="/knowledge-base" className={({ isActive }) => (isActive ? "active" : "")}>
            知识库
          </NavLink>
        </nav>
      </header>
      <main className="app-main">
        <Routes>
          <Route path="/" element={<ChatPage messages={messages} setMessages={setMessages} />} />
          <Route path="/knowledge-base" element={<KnowledgeBasePage />} />
        </Routes>
      </main>
    </div>
  );
}

export default App;
