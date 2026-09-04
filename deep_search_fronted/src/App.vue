<script setup lang="ts">
import { ref, onMounted, nextTick, computed } from 'vue'
import axios from 'axios'
import { marked } from 'marked'

// Types
interface Message {
  role: 'user' | 'ai' | 'system'
  content: string
  logs?: LogItem[]
  files?: FileItem[]
  timestamp?: number
}

interface LogItem {
  type: string
  title: string
  details: any
  timestamp: string
}

interface FileItem {
  name: string
  path: string
  url: string
}

// State
const inputQuery = ref('')
const messages = ref<Message[]>([])
const status = ref<'idle' | 'running'>('idle')
const socket = ref<WebSocket | null>(null)
const currentSessionPath = ref('')
const currentSessionUrl = ref('')
const messagesEndRef = ref<HTMLElement | null>(null)
const isWelcomeScreen = computed(() => messages.value.length === 0)
const isSidebarOpen = ref(false)
const fileList = ref<any[]>([])
const currentThreadId = ref(crypto.randomUUID())

// Theme State
const isDarkMode = ref(true)

// Toggle Theme
const toggleTheme = () => {
  isDarkMode.value = !isDarkMode.value
  document.documentElement.setAttribute('data-theme', isDarkMode.value ? 'dark' : 'light')
  localStorage.setItem('theme', isDarkMode.value ? 'dark' : 'light')
}

// Helper: Scroll to bottom
const scrollToBottom = async () => {
  await nextTick()
  if (messagesEndRef.value) {
    messagesEndRef.value.scrollIntoView({ behavior: 'smooth' })
  }
}

// Fetch Files
const fetchFiles = async () => {
  if (!currentSessionPath.value) return
  try {
    const res = await axios.get('http://localhost:8000/api/files', {
      params: { path: currentSessionPath.value }
    })
    if (res.data.files) {
      fileList.value = res.data.files.map((f: any) => ({
        ...f,
        url: `http://localhost:8000/api/download?path=${encodeURIComponent(f.path)}`
      }))
    }
  } catch (e) {
    console.error('Failed to fetch files', e)
  }
}

// WebSocket Connection
const connectWebSocket = () => {
  const ws = new WebSocket(`ws://localhost:8000/ws/${currentThreadId.value}`)

  ws.onopen = () => {
    console.log('WebSocket Connected')
  }

  ws.onmessage = (event) => {
    try {
      const data = JSON.parse(event.data)
      handleSocketMessage(data)
    } catch (e) {
      console.error('Error parsing WS message:', e)
    }
  }

  ws.onclose = () => {
    console.log('WebSocket Disconnected, retrying in 3s...')
    setTimeout(connectWebSocket, 3000)
  }

  socket.value = ws
}

// Handle Incoming Messages
const handleSocketMessage = (data: any) => {
  const { type, event, message, data: eventData } = data

  if (type === 'pong') return

  let lastAiMsg = messages.value.slice().reverse().find(m => m.role === 'ai')
  
  if (event === 'session_created') {
    currentSessionPath.value = eventData.path
    const parts = eventData.path.split(/output[\\/]/)
    if (parts.length > 1) {
      currentSessionUrl.value = `http://localhost:8000/outputs/${parts[1].replace(/\\/g, '/')}`
    }
    isSidebarOpen.value = true
    fetchFiles()
  } else if (event === 'tool_start') {
    if (currentSessionPath.value) {
      fetchFiles()
      setTimeout(fetchFiles, 2000)
    }

    if (lastAiMsg) {
      if (!lastAiMsg.logs) lastAiMsg.logs = []
      lastAiMsg.logs.push({
        type: 'tool',
        title: `使用的工具： ${eventData.tool_name}...`,
        details: eventData.args,
        timestamp: new Date().toLocaleTimeString()
      })
      
      if (eventData.args && eventData.args.filename && currentSessionUrl.value) {
        if (!lastAiMsg.files) lastAiMsg.files = []
        const fileUrl = `${currentSessionUrl.value}/${eventData.args.filename}`
        if (!lastAiMsg.files.find(f => f.name === eventData.args.filename)) {
           lastAiMsg.files.push({
            name: eventData.args.filename,
            path: eventData.args.filename,
            url: fileUrl
          })
        }
      }
    }
  } else if (event === 'assistant_call') {
    if (currentSessionPath.value) {
        fetchFiles()
    }
     if (lastAiMsg) {
      if (!lastAiMsg.logs) lastAiMsg.logs = []
      lastAiMsg.logs.push({
        type: 'agent',
        title: `正在使用助手： ${eventData.assistant_name}...`,
        details: eventData.args,
        timestamp: new Date().toLocaleTimeString()
      })
    }
  } else if (event === 'task_result') {
    if (lastAiMsg) {
      lastAiMsg.content = eventData.result
    } else {
       messages.value.push({
        role: 'ai',
        content: eventData.result,
        timestamp: Date.now()
      })
    }
    status.value = 'idle'
    fetchFiles()
  } else if (event === 'error') {
     messages.value.push({
      role: 'system',
      content: `Error: ${message}`,
      timestamp: Date.now()
    })
    status.value = 'idle'
  }
  
  scrollToBottom()
}

// Send Message
const sendMessage = async () => {
  if ((!inputQuery.value.trim() && selectedFiles.value.length === 0) || status.value === 'running') return

  const query = inputQuery.value
  inputQuery.value = ''
  status.value = 'running'

  messages.value.push({
    role: 'user',
    content: query,
    timestamp: Date.now()
  })

  messages.value.push({
    role: 'ai',
    content: '',
    logs: [],
    files: [],
    timestamp: Date.now()
  })

  scrollToBottom()

  // Handle File Upload
  if (selectedFiles.value.length > 0) {
    console.log('Uploading files:', selectedFiles.value)
    
    const lastAiMsg = messages.value[messages.value.length - 1]
    if (lastAiMsg && lastAiMsg.role === 'ai') {
        if (!lastAiMsg.logs) lastAiMsg.logs = []
        
        const fileDetails = selectedFiles.value.map(f => ({ name: f.name, size: f.size }))
        
        lastAiMsg.logs.push({
            type: 'info',
            title: `Uploading ${selectedFiles.value.length} file(s)...`,
            details: fileDetails,
            timestamp: new Date().toLocaleTimeString()
        })
    }

    try {
        const formData = new FormData()
        if (typeof currentThreadId !== 'undefined' && currentThreadId.value) {
             formData.append('thread_id', currentThreadId.value)
        }

        selectedFiles.value.forEach(file => {
            formData.append('files', file)
        })

        await axios.post('http://127.0.0.1:8000/api/upload', formData, {
            headers: {
                'Content-Type': 'multipart/form-data'
            }
        })
        
        selectedFiles.value = []
        
        if (lastAiMsg && lastAiMsg.logs) {
            lastAiMsg.logs.push({
                type: 'success',
                title: 'Files uploaded successfully',
                details: null,
                timestamp: new Date().toLocaleTimeString()
            })
        }

    } catch (e: any) {
        console.error('Upload failed', e)
        if (lastAiMsg && lastAiMsg.logs) {
            lastAiMsg.logs.push({
                type: 'error',
                title: 'File upload failed',
                details: e.message || 'Unknown error',
                timestamp: new Date().toLocaleTimeString()
            })
        }
    }
  }

  try {
    const payload: any = { query }
    if (typeof currentThreadId !== 'undefined' && currentThreadId.value) {
      payload.thread_id = currentThreadId.value
    }
    const res = await axios.post('http://127.0.0.1:8000/api/task', payload)
    
    if (res.data && res.data.thread_id) {
      currentThreadId.value = res.data.thread_id
    }
  } catch (error: any) {
    console.error('Request failed:', error)
    let errorMsg = 'Failed to send request.'
    if (error.message) errorMsg += ` (${error.message})`
    if (error.response && error.response.data) {
        errorMsg += ` Server says: ${JSON.stringify(error.response.data)}`
    }
    
    messages.value.push({
      role: 'system',
      content: errorMsg,
      timestamp: Date.now()
    })
    status.value = 'idle'
  }
}

// File Upload
const fileInputRef = ref<HTMLInputElement | null>(null)
const selectedFiles = ref<File[]>([])

const triggerFileUpload = () => {
  fileInputRef.value?.click()
}

const handleFileChange = (event: Event) => {
  const target = event.target as HTMLInputElement
  if (target.files && target.files.length > 0) {
    selectedFiles.value = [...selectedFiles.value, ...Array.from(target.files)]
    target.value = ''
  }
}

const removeFile = (index: number) => {
  selectedFiles.value.splice(index, 1)
}

const renderMarkdown = (text: string) => {
  if (!text) return '<span class="typing-indicator">Thinking...</span>'
  return marked(text)
}

onMounted(() => {
  const savedTheme = localStorage.getItem('theme')
  if (savedTheme) {
    isDarkMode.value = savedTheme === 'dark'
  }
  document.documentElement.setAttribute('data-theme', isDarkMode.value ? 'dark' : 'light')
  connectWebSocket()
})
</script>

<template>
  <div class="app-container">
    <!-- Ambient Background -->
    <div class="ambient-bg">
      <div class="ambient-orb orb-1"></div>
      <div class="ambient-orb orb-2"></div>
      <div class="ambient-orb orb-3"></div>
    </div>

    <!-- Main Content -->
    <main class="main-content" :class="{ 'centered-layout': isWelcomeScreen }">
      
      <!-- Top Action Buttons -->
      <div class="top-actions">
        <!-- Theme Toggle Button -->
        <button class="top-action-btn" @click="toggleTheme" :title="isDarkMode ? '切换到亮色模式' : '切换到暗色模式'">
          <!-- Sun icon for light mode -->
          <svg v-if="isDarkMode" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
            <circle cx="12" cy="12" r="5"/>
            <line x1="12" y1="1" x2="12" y2="3"/>
            <line x1="12" y1="21" x2="12" y2="23"/>
            <line x1="4.22" y1="4.22" x2="5.64" y2="5.64"/>
            <line x1="18.36" y1="18.36" x2="19.78" y2="19.78"/>
            <line x1="1" y1="12" x2="3" y2="12"/>
            <line x1="21" y1="12" x2="23" y2="12"/>
            <line x1="4.22" y1="19.78" x2="5.64" y2="18.36"/>
            <line x1="18.36" y1="5.64" x2="19.78" y2="4.22"/>
          </svg>
          <!-- Moon icon for dark mode -->
          <svg v-else width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
            <path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"/>
          </svg>
        </button>

        <!-- Sidebar Toggle Button -->
        <button 
          v-if="currentSessionPath && !isSidebarOpen" 
          class="top-action-btn" 
          @click="isSidebarOpen = true"
          title="Open File Sidebar"
        >
          <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
            <path d="M4 6H20M4 12H20M4 18H20"/>
          </svg>
        </button>
      </div>

      <!-- Welcome Screen -->
      <div v-if="isWelcomeScreen" class="welcome-screen">
        <div class="welcome-content">
          <div class="welcome-logo">
            <div class="logo-ring">
              <svg width="48" height="48" viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg">
                <path d="M12 2L14.5 9.5L22 12L14.5 14.5L12 22L9.5 14.5L2 12L9.5 9.5L12 2Z" fill="url(#grad-welcome)"/>
                <defs>
                  <linearGradient id="grad-welcome" x1="2" y1="2" x2="22" y2="22" gradientUnits="userSpaceOnUse">
                    <stop stop-color="#6366F1"/>
                    <stop offset="1" stop-color="#EC4899"/>
                  </linearGradient>
                </defs>
              </svg>
            </div>
          </div>
          <h1 class="welcome-title">Deep Search</h1>
          <h2 class="welcome-subtitle">多智能体协作，深度探索信息</h2>
        </div>
      </div>

      <!-- Chat Area -->
      <div v-else class="chat-scroll-area">
        <div class="chat-container">
          <div v-for="(msg, index) in messages" :key="index" class="message-wrapper" :class="msg.role">
            
            <!-- User Message -->
            <div v-if="msg.role === 'user'" class="message-user">
              <div class="msg-content">{{ msg.content }}</div>
            </div>

            <!-- AI Message -->
            <div v-else-if="msg.role === 'ai'" class="message-ai">
              <div class="ai-avatar">
                <svg width="22" height="22" viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg">
                  <path d="M12 2L14.5 9.5L22 12L14.5 14.5L12 22L9.5 14.5L2 12L9.5 9.5L12 2Z" fill="url(#grad-ai)"/>
                  <defs>
                    <linearGradient id="grad-ai" x1="2" y1="2" x2="22" y2="22" gradientUnits="userSpaceOnUse">
                      <stop stop-color="#6366F1"/>
                      <stop offset="1" stop-color="#EC4899"/>
                    </linearGradient>
                  </defs>
                </svg>
              </div>
              
              <div class="ai-content-wrapper">
                <!-- Logs / Thinking Process -->
                <div v-if="msg.logs && msg.logs.length > 0" class="process-section">
                  <details>
                    <summary>
                      <span class="spinner" v-if="status === 'running' && index === messages.length - 1"></span>
                      <span>View thought process</span>
                      <svg class="chevron-icon" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="6 9 12 15 18 9"/></svg>
                    </summary>
                    <div class="process-steps">
                      <div v-for="(log, idx) in msg.logs" :key="idx" class="step-item">
                        <div class="step-header">
                          <span class="step-dot"></span>
                          <span class="step-title">{{ log.title }}</span>
                        </div>
                        <div class="step-details" v-if="log.details">
                           <pre>{{ JSON.stringify(log.details, null, 2) }}</pre>
                        </div>
                      </div>
                    </div>
                  </details>
                </div>

                <!-- Text Content -->
                <div class="markdown-body" v-html="renderMarkdown(msg.content)"></div>

                <!-- Files -->
                <div v-if="msg.files && msg.files.length > 0" class="files-grid">
                  <a v-for="file in msg.files" :key="file.name" :href="file.url" target="_blank" class="file-card" :download="file.name">
                    <div class="file-card-icon">
                      <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5">
                        <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/>
                        <polyline points="14 2 14 8 20 8"/>
                      </svg>
                    </div>
                    <div class="file-info">
                      <div class="file-name">{{ file.name }}</div>
                      <div class="file-type">Click to download</div>
                    </div>
                  </a>
                </div>
              </div>
            </div>

            <!-- System Message -->
             <div v-else class="message-system">
              <div class="system-msg-content">{{ msg.content }}</div>
            </div>

          </div>
          <div ref="messagesEndRef" class="spacer-bottom"></div>
        </div>
      </div>

      <!-- Input Area -->
      <footer class="input-footer">
        <!-- File Preview Tab -->
        <div v-if="selectedFiles.length > 0" class="file-preview-container">
          <div v-for="(file, index) in selectedFiles" :key="index" class="file-preview-chip">
            <svg class="file-preview-icon" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
              <path d="M21.44 11.05l-9.19 9.19a6 6 0 0 1-8.49-8.49l9.19-9.19a4 4 0 0 1 5.66 5.66l-9.2 9.19a2 2 0 0 1-2.83-2.83l8.49-8.48"/>
            </svg>
            <span class="file-preview-name">{{ file.name }}</span>
            <button class="file-remove-btn" @click="removeFile(index)" title="Remove file">
              <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
            </button>
          </div>
        </div>

        <div class="input-container" :class="{ focused: status === 'running' }">
          <input 
            type="file" 
            ref="fileInputRef" 
            multiple
            style="display: none" 
            @change="handleFileChange" 
          />
          <button class="icon-btn upload-btn" @click="triggerFileUpload" :disabled="status === 'running'" title="Upload file">
            <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5">
              <path d="M21.44 11.05l-9.19 9.19a6 6 0 0 1-8.49-8.49l9.19-9.19a4 4 0 0 1 5.66 5.66l-9.2 9.19a2 2 0 0 1-2.83-2.83l8.49-8.48"/>
            </svg>
          </button>
          <textarea 
            v-model="inputQuery" 
            @keydown.enter.exact.prevent="sendMessage"
            placeholder="Enter a prompt here"
            :disabled="status === 'running'"
            rows="1"
          ></textarea>
          <button class="icon-btn send-btn" :class="{ active: inputQuery.trim() || status === 'running' }" @click="sendMessage" :disabled="!inputQuery.trim() && status !== 'running'">
            <svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="2">
              <line x1="22" y1="2" x2="11" y2="13"/>
              <polygon points="22 2 15 22 11 13 2 9 22 2"/>
            </svg>
          </button>
        </div>
        <div class="footer-text">
          DeepAgents may display inaccurate info, including about people, so double-check its responses.
        </div>
      </footer>
    </main>

    <!-- Right Sidebar (File Explorer) -->
    <aside v-if="isSidebarOpen" class="file-sidebar">
      <div class="sidebar-header">
        <div class="sidebar-title-group">
          <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5">
            <path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/>
          </svg>
          <h3>Session Files</h3>
        </div>
        <div class="sidebar-actions">
            <button class="icon-btn refresh-btn" @click="fetchFiles" title="Refresh Files">
              <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                <polyline points="23 4 23 10 17 10"/>
                <path d="M20.49 15a9 9 0 1 1-2.12-9.36L23 10"/>
              </svg>
            </button>
            <button class="icon-btn close-btn" @click="isSidebarOpen = false">
              <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
            </button>
        </div>
      </div>
      <div class="file-list">
        <div v-if="fileList.length === 0" class="empty-files">
          <svg width="40" height="40" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1" opacity="0.3">
            <path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/>
          </svg>
          <p>No files generated yet.</p>
        </div>
        <div v-else v-for="file in fileList" :key="file.path" class="file-item">
          <a :href="file.url" target="_blank" class="file-link" :download="file.name">
            <div class="file-link-icon">
              <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5">
                <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/>
                <polyline points="14 2 14 8 20 8"/>
              </svg>
            </div>
            <span class="file-name-text">{{ file.name }}</span>
            <svg class="download-icon" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
              <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/>
              <polyline points="7 10 12 15 17 10"/>
              <line x1="12" y1="15" x2="12" y2="3"/>
            </svg>
          </a>
        </div>
      </div>
    </aside>
  </div>
</template>

<style>
/* ===== Global Variables - Dark Theme (Default) ===== */
:root,
[data-theme="dark"] {
  --bg-primary: #0a0a0f;
  --bg-secondary: #12121a;
  --bg-surface: #1a1a26;
  --bg-elevated: #222233;
  --bg-hover: #2a2a3d;

  --text-primary: #f0f0f5;
  --text-secondary: #9898b0;
  --text-muted: #5a5a72;

  --accent-start: #6366F1;
  --accent-end: #EC4899;
  --accent-glow: rgba(99, 102, 241, 0.15);

  --user-msg-bg: linear-gradient(135deg, #6366F1, #818CF8);
  --border-subtle: rgba(255, 255, 255, 0.06);
  --border-light: rgba(255, 255, 255, 0.1);

  --shadow-sm: 0 2px 8px rgba(0, 0, 0, 0.2);
  --shadow-md: 0 4px 16px rgba(0, 0, 0, 0.3);
  --shadow-lg: 0 8px 32px rgba(0, 0, 0, 0.4);

  --orb-opacity: 0.35;
  --code-color: #c9a0dc;
}

/* ===== Light Theme ===== */
[data-theme="light"] {
  --bg-primary: #ffffff;
  --bg-secondary: #f8f9fa;
  --bg-surface: #f0f1f3;
  --bg-elevated: #e8e9eb;
  --bg-hover: #dcdde0;

  --text-primary: #1a1a2e;
  --text-secondary: #5a5a72;
  --text-muted: #8a8aa0;

  --accent-start: #4f46e5;
  --accent-end: #db2777;
  --accent-glow: rgba(79, 70, 229, 0.1);

  --user-msg-bg: linear-gradient(135deg, #4f46e5, #6366f1);
  --border-subtle: rgba(0, 0, 0, 0.06);
  --border-light: rgba(0, 0, 0, 0.1);

  --shadow-sm: 0 2px 8px rgba(0, 0, 0, 0.08);
  --shadow-md: 0 4px 16px rgba(0, 0, 0, 0.1);
  --shadow-lg: 0 8px 32px rgba(0, 0, 0, 0.12);

  --orb-opacity: 0.15;
  --code-color: #7c3aed;
}

/* ===== Shared Variables ===== */
:root {
  --radius-sm: 8px;
  --radius-md: 12px;
  --radius-lg: 16px;
  --radius-xl: 24px;
  --radius-full: 9999px;

  --transition-fast: 0.15s cubic-bezier(0.4, 0, 0.2, 1);
  --transition-normal: 0.25s cubic-bezier(0.4, 0, 0.2, 1);
  --transition-slow: 0.4s cubic-bezier(0.4, 0, 0.2, 1);
}

/* ===== Base ===== */
*, *::before, *::after {
  box-sizing: border-box;
}

body {
  margin: 0;
  background-color: var(--bg-primary);
  color: var(--text-primary);
  font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', 'Inter', 'Helvetica Neue', sans-serif;
  overflow: hidden;
  -webkit-font-smoothing: antialiased;
  -moz-osx-font-smoothing: grayscale;
}

#app {
  width: 100%;
  height: 100%;
}

/* ===== Ambient Background ===== */
.ambient-bg {
  position: fixed;
  inset: 0;
  z-index: 0;
  pointer-events: none;
  overflow: hidden;
}

.ambient-orb {
  position: absolute;
  border-radius: 50%;
  filter: blur(120px);
  opacity: var(--orb-opacity);
  transition: opacity var(--transition-slow);
}

.orb-1 {
  width: 600px;
  height: 600px;
  background: radial-gradient(circle, rgba(99, 102, 241, 0.3), transparent 70%);
  top: -200px;
  left: -100px;
  animation: floatOrb1 20s ease-in-out infinite;
}

.orb-2 {
  width: 500px;
  height: 500px;
  background: radial-gradient(circle, rgba(236, 72, 153, 0.2), transparent 70%);
  bottom: -150px;
  right: -100px;
  animation: floatOrb2 25s ease-in-out infinite;
}

.orb-3 {
  width: 400px;
  height: 400px;
  background: radial-gradient(circle, rgba(99, 102, 241, 0.15), transparent 70%);
  top: 50%;
  left: 50%;
  transform: translate(-50%, -50%);
  animation: floatOrb3 30s ease-in-out infinite;
}

@keyframes floatOrb1 {
  0%, 100% { transform: translate(0, 0); }
  33% { transform: translate(80px, 60px); }
  66% { transform: translate(-40px, 30px); }
}

@keyframes floatOrb2 {
  0%, 100% { transform: translate(0, 0); }
  33% { transform: translate(-60px, -40px); }
  66% { transform: translate(40px, -60px); }
}

@keyframes floatOrb3 {
  0%, 100% { transform: translate(-50%, -50%) scale(1); }
  50% { transform: translate(-50%, -50%) scale(1.2); }
}

/* ===== Layout ===== */
.app-container {
  display: flex;
  height: 100vh;
  width: 100vw;
  position: relative;
  z-index: 1;
}

.main-content {
  flex: 1;
  display: flex;
  flex-direction: column;
  position: relative;
  min-width: 0;
}

/* ===== Top Actions ===== */
.top-actions {
  position: absolute;
  top: 1rem;
  right: 1rem;
  display: flex;
  gap: 0.5rem;
  z-index: 10;
}

.top-action-btn {
  background: var(--bg-surface);
  border: 1px solid var(--border-light);
  color: var(--text-secondary);
  cursor: pointer;
  padding: 10px;
  border-radius: var(--radius-md);
  display: flex;
  align-items: center;
  justify-content: center;
  transition: all var(--transition-fast);
  backdrop-filter: blur(12px);
}

.top-action-btn:hover {
  background: var(--bg-elevated);
  color: var(--text-primary);
  border-color: var(--border-light);
  box-shadow: var(--shadow-sm);
  transform: scale(1.05);
}

.top-action-btn:active {
  transform: scale(0.95);
}

/* ===== Welcome Screen ===== */
.welcome-screen {
  flex: 1;
  display: flex;
  flex-direction: column;
  justify-content: center;
  align-items: center;
  padding: 2rem;
  animation: fadeInUp 0.6s ease-out;
}

@keyframes fadeInUp {
  from { opacity: 0; transform: translateY(20px); }
  to { opacity: 1; transform: translateY(0); }
}

.welcome-content {
  text-align: center;
  max-width: 600px;
}

.welcome-logo {
  margin-bottom: 2rem;
  display: flex;
  justify-content: center;
}

.logo-ring {
  width: 88px;
  height: 88px;
  border-radius: 50%;
  display: flex;
  align-items: center;
  justify-content: center;
  background: linear-gradient(135deg, rgba(99, 102, 241, 0.1), rgba(236, 72, 153, 0.1));
  border: 1px solid rgba(99, 102, 241, 0.2);
  animation: pulseGlow 3s ease-in-out infinite;
}

@keyframes pulseGlow {
  0%, 100% { box-shadow: 0 0 20px rgba(99, 102, 241, 0.1); }
  50% { box-shadow: 0 0 40px rgba(99, 102, 241, 0.2), 0 0 60px rgba(236, 72, 153, 0.1); }
}

.welcome-title {
  font-size: 3rem;
  font-weight: 700;
  background: linear-gradient(135deg, var(--accent-start), var(--accent-end));
  -webkit-background-clip: text;
  -webkit-text-fill-color: transparent;
  background-clip: text;
  margin: 0 0 0.5rem;
  letter-spacing: -0.02em;
  line-height: 1.2;
}

.welcome-subtitle {
  font-size: 1.5rem;
  font-weight: 400;
  color: var(--text-muted);
  margin: 0 0 2.5rem;
  letter-spacing: -0.01em;
}

/* ===== Centered Layout ===== */
.main-content.centered-layout {
  justify-content: center;
  align-items: center;
  overflow-y: auto;
}

.main-content.centered-layout .welcome-screen {
  flex: 0 0 auto;
  padding-bottom: 2rem;
}

.main-content.centered-layout .input-footer {
  width: 100%;
  max-width: 100%;
  padding: 0;
  background: transparent;
  justify-content: center;
}

/* ===== Chat Area ===== */
.chat-scroll-area {
  flex: 1;
  overflow-y: auto;
  padding: 1.5rem 1rem;
}

.chat-container {
  max-width: 820px;
  margin: 0 auto;
  display: flex;
  flex-direction: column;
  gap: 1.5rem;
}

.message-wrapper {
  display: flex;
  flex-direction: column;
  width: 100%;
  animation: messageIn 0.3s ease-out;
}

@keyframes messageIn {
  from { opacity: 0; transform: translateY(8px); }
  to { opacity: 1; transform: translateY(0); }
}

/* ===== User Message ===== */
.message-user {
  align-self: flex-end;
  max-width: 70%;
}

.msg-content {
  background: var(--user-msg-bg);
  padding: 12px 20px;
  border-radius: var(--radius-xl);
  border-bottom-right-radius: var(--radius-sm);
  line-height: 1.6;
  font-size: 0.95rem;
  color: #fff;
  box-shadow: 0 2px 12px rgba(99, 102, 241, 0.25);
}

/* ===== AI Message ===== */
.message-ai {
  align-self: flex-start;
  width: 100%;
  display: flex;
  gap: 0.875rem;
}

.ai-avatar {
  flex-shrink: 0;
  width: 36px;
  height: 36px;
  margin-top: 2px;
  border-radius: var(--radius-md);
  background: linear-gradient(135deg, rgba(99, 102, 241, 0.12), rgba(236, 72, 153, 0.12));
  border: 1px solid rgba(99, 102, 241, 0.15);
  display: flex;
  align-items: center;
  justify-content: center;
}

.ai-content-wrapper {
  flex: 1;
  min-width: 0;
}

.markdown-body {
  line-height: 1.7;
  font-size: 0.95rem;
  color: var(--text-primary);
}

.markdown-body p {
  margin: 0.5em 0;
}

.markdown-body h1, .markdown-body h2, .markdown-body h3 {
  margin-top: 1.2em;
  margin-bottom: 0.6em;
  font-weight: 600;
}

.markdown-body code {
  background: var(--bg-elevated);
  padding: 0.15em 0.4em;
  border-radius: 4px;
  font-size: 0.88em;
  font-family: 'SF Mono', 'Fira Code', monospace;
  color: var(--code-color);
}

.markdown-body pre {
  background: var(--bg-secondary);
  padding: 1rem 1.25rem;
  border-radius: var(--radius-md);
  overflow-x: auto;
  border: 1px solid var(--border-subtle);
  margin: 0.75em 0;
}

.markdown-body pre code {
  background: none;
  padding: 0;
  color: var(--text-primary);
  font-size: 0.85rem;
}

.markdown-body a {
  color: var(--accent-start);
  text-decoration: none;
}

.markdown-body a:hover {
  text-decoration: underline;
}

.markdown-body ul, .markdown-body ol {
  padding-left: 1.5em;
}

.markdown-body blockquote {
  border-left: 3px solid var(--accent-start);
  padding-left: 1em;
  margin-left: 0;
  color: var(--text-secondary);
}

.typing-indicator {
  color: var(--text-muted);
  font-style: italic;
  animation: pulse 1.5s ease-in-out infinite;
}

@keyframes pulse {
  0%, 100% { opacity: 0.4; }
  50% { opacity: 1; }
}

/* ===== Process / Logs ===== */
.process-section {
  margin-bottom: 0.75rem;
}

.process-section details {
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius-md);
  overflow: hidden;
}

.process-section summary {
  cursor: pointer;
  color: var(--text-secondary);
  font-size: 0.8rem;
  list-style: none;
  display: flex;
  align-items: center;
  gap: 0.5rem;
  padding: 0.6rem 0.875rem;
  background: var(--bg-surface);
  transition: background var(--transition-fast);
  user-select: none;
}

.process-section summary::-webkit-details-marker {
  display: none;
}

.process-section summary:hover {
  background: var(--bg-elevated);
}

.process-section details[open] summary {
  border-bottom: 1px solid var(--border-subtle);
}

.chevron-icon {
  margin-left: auto;
  transition: transform var(--transition-fast);
  opacity: 0.5;
}

details[open] .chevron-icon {
  transform: rotate(180deg);
}

.spinner {
  width: 12px;
  height: 12px;
  border: 2px solid var(--border-light);
  border-top-color: var(--accent-start);
  border-radius: 50%;
  animation: spin 0.8s linear infinite;
}

@keyframes spin { to { transform: rotate(360deg); } }

.process-steps {
  background: var(--bg-secondary);
  padding: 0.75rem;
  max-height: 300px;
  overflow-y: auto;
}

.step-item {
  padding: 0.5rem 0.75rem;
  border-left: 2px solid var(--border-light);
  margin-left: 0.25rem;
  margin-bottom: 0.5rem;
  transition: border-color var(--transition-fast);
}

.step-item:hover {
  border-left-color: var(--accent-start);
}

.step-item:last-child {
  margin-bottom: 0;
}

.step-header {
  font-size: 0.8rem;
  font-weight: 500;
  color: var(--text-primary);
  display: flex;
  align-items: center;
  gap: 0.5rem;
}

.step-dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: var(--accent-start);
  flex-shrink: 0;
}

.step-details pre {
  margin: 0.5rem 0 0 0;
  font-size: 0.72rem;
  color: var(--text-muted);
  background: var(--bg-primary);
  padding: 0.6rem;
  border-radius: var(--radius-sm);
  overflow-x: auto;
  border: 1px solid var(--border-subtle);
}

/* ===== Files Grid ===== */
.files-grid {
  display: flex;
  flex-wrap: wrap;
  gap: 0.625rem;
  margin-top: 1rem;
}

.file-card {
  display: flex;
  align-items: center;
  gap: 0.75rem;
  background: var(--bg-surface);
  padding: 0.75rem 1rem;
  border-radius: var(--radius-md);
  text-decoration: none;
  color: var(--text-primary);
  border: 1px solid var(--border-light);
  transition: all var(--transition-fast);
  min-width: 160px;
}

.file-card:hover {
  background: var(--bg-elevated);
  border-color: rgba(99, 102, 241, 0.3);
  transform: translateY(-1px);
  box-shadow: var(--shadow-sm);
}

.file-card-icon {
  width: 36px;
  height: 36px;
  border-radius: var(--radius-sm);
  background: linear-gradient(135deg, rgba(99, 102, 241, 0.12), rgba(236, 72, 153, 0.08));
  display: flex;
  align-items: center;
  justify-content: center;
  color: var(--accent-start);
  flex-shrink: 0;
}

.file-info {
  display: flex;
  flex-direction: column;
  min-width: 0;
}

.file-name {
  font-weight: 500;
  font-size: 0.85rem;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.file-type {
  font-size: 0.72rem;
  color: var(--text-muted);
}

/* ===== System Message ===== */
.message-system {
  text-align: center;
  margin: 0.5rem 0;
}

.system-msg-content {
  display: inline-flex;
  align-items: center;
  gap: 0.5rem;
  font-size: 0.8rem;
  color: #ef4444;
  background: rgba(239, 68, 68, 0.08);
  border: 1px solid rgba(239, 68, 68, 0.15);
  padding: 0.5rem 1rem;
  border-radius: var(--radius-full);
}

.spacer-bottom { height: 80px; }

/* ===== Input Footer ===== */
.input-footer {
  background: linear-gradient(to top, var(--bg-primary) 60%, transparent);
  padding: 1rem 2rem 1.5rem;
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 0.625rem;
}

.input-container {
  width: 100%;
  max-width: 820px;
  background: var(--bg-surface);
  border-radius: var(--radius-xl);
  display: flex;
  align-items: center;
  padding: 0.375rem 0.5rem 0.375rem 0.75rem;
  border: 1px solid var(--border-light);
  transition: all var(--transition-normal);
  backdrop-filter: blur(12px);
}

.input-container:focus-within {
  border-color: rgba(99, 102, 241, 0.4);
  box-shadow: 0 0 0 3px rgba(99, 102, 241, 0.08), var(--shadow-md);
  background: var(--bg-elevated);
}

.input-container.focused {
  border-color: rgba(99, 102, 241, 0.3);
}

textarea {
  flex: 1;
  background: transparent;
  border: none;
  color: var(--text-primary);
  font-size: 0.95rem;
  padding: 0 8px;
  resize: none;
  height: 40px;
  max-height: 200px;
  font-family: inherit;
  outline: none;
  line-height: 40px;
}

textarea::placeholder {
  color: var(--text-muted);
}

/* ===== Icon Buttons ===== */
.icon-btn {
  background: none;
  border: none;
  color: var(--text-muted);
  cursor: pointer;
  padding: 8px;
  border-radius: var(--radius-md);
  display: flex;
  align-items: center;
  justify-content: center;
  transition: all var(--transition-fast);
  flex-shrink: 0;
}

.icon-btn:hover:not(:disabled) {
  background: var(--bg-hover);
  color: var(--text-primary);
}

.icon-btn:disabled {
  opacity: 0.3;
  cursor: default;
}

.send-btn {
  background: linear-gradient(135deg, var(--accent-start), var(--accent-end));
  color: #fff;
  border-radius: var(--radius-lg);
  padding: 8px 12px;
}

.send-btn.active {
  box-shadow: 0 2px 12px rgba(99, 102, 241, 0.35);
}

.send-btn.active:hover {
  background: linear-gradient(135deg, #7c7ff7, #f06aab);
  transform: scale(1.03);
}

.send-btn:not(.active) {
  background: var(--bg-hover);
  color: var(--text-muted);
  box-shadow: none;
}

.footer-text {
  font-size: 0.7rem;
  color: var(--text-muted);
  text-align: center;
  opacity: 0.6;
}

/* ===== File Preview ===== */
.file-preview-container {
  width: 100%;
  max-width: 820px;
  display: flex;
  justify-content: flex-start;
  padding-left: 0.5rem;
  flex-wrap: wrap;
  gap: 0.5rem;
}

.file-preview-chip {
  display: flex;
  align-items: center;
  gap: 0.5rem;
  background: var(--bg-surface);
  padding: 0.4rem 0.5rem 0.4rem 0.75rem;
  border-radius: var(--radius-full);
  border: 1px solid var(--border-light);
  font-size: 0.8rem;
  color: var(--text-secondary);
  animation: chipIn 0.2s ease-out;
}

@keyframes chipIn {
  from { opacity: 0; transform: scale(0.95) translateY(4px); }
  to { opacity: 1; transform: scale(1) translateY(0); }
}

.file-preview-icon {
  color: var(--accent-start);
}

.file-preview-name {
  max-width: 180px;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.file-remove-btn {
  background: none;
  border: none;
  color: var(--text-muted);
  cursor: pointer;
  padding: 4px;
  border-radius: var(--radius-sm);
  display: flex;
  align-items: center;
  justify-content: center;
  transition: all var(--transition-fast);
}

.file-remove-btn:hover {
  background: rgba(239, 68, 68, 0.15);
  color: #ef4444;
}

/* ===== File Sidebar ===== */
.file-sidebar {
  width: 300px;
  background: var(--bg-secondary);
  border-left: 1px solid var(--border-subtle);
  display: flex;
  flex-direction: column;
  flex-shrink: 0;
  backdrop-filter: blur(16px);
  animation: slideIn 0.3s ease-out;
}

@keyframes slideIn {
  from { opacity: 0; transform: translateX(20px); }
  to { opacity: 1; transform: translateX(0); }
}

.sidebar-header {
  padding: 1rem 1.25rem;
  border-bottom: 1px solid var(--border-subtle);
  display: flex;
  justify-content: space-between;
  align-items: center;
}

.sidebar-title-group {
  display: flex;
  align-items: center;
  gap: 0.625rem;
  color: var(--text-secondary);
}

.sidebar-header h3 {
  margin: 0;
  font-size: 0.9rem;
  font-weight: 600;
  color: var(--text-primary);
}

.sidebar-actions {
  display: flex;
  gap: 4px;
}

.file-list {
  flex: 1;
  overflow-y: auto;
  padding: 0.75rem;
}

.empty-files {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 0.75rem;
  color: var(--text-muted);
  text-align: center;
  padding: 2rem 1rem;
}

.empty-files p {
  margin: 0;
  font-size: 0.85rem;
}

.file-item {
  margin-bottom: 0.25rem;
}

.file-link {
  display: flex;
  align-items: center;
  gap: 0.625rem;
  padding: 0.625rem 0.75rem;
  border-radius: var(--radius-md);
  color: var(--text-primary);
  text-decoration: none;
  transition: all var(--transition-fast);
  border: 1px solid transparent;
}

.file-link:hover {
  background: var(--bg-elevated);
  border-color: var(--border-light);
}

.file-link:hover .download-icon {
  opacity: 1;
}

.file-link-icon {
  width: 32px;
  height: 32px;
  border-radius: var(--radius-sm);
  background: var(--bg-surface);
  display: flex;
  align-items: center;
  justify-content: center;
  color: var(--accent-start);
  flex-shrink: 0;
}

.file-name-text {
  flex: 1;
  font-size: 0.85rem;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  min-width: 0;
}

.download-icon {
  opacity: 0;
  color: var(--text-muted);
  transition: opacity var(--transition-fast);
  flex-shrink: 0;
}

/* ===== Scrollbar ===== */
::-webkit-scrollbar {
  width: 6px;
  height: 6px;
}

::-webkit-scrollbar-track {
  background: transparent;
}

::-webkit-scrollbar-thumb {
  background: rgba(255, 255, 255, 0.08);
  border-radius: 3px;
}

::-webkit-scrollbar-thumb:hover {
  background: rgba(255, 255, 255, 0.15);
}

/* ===== Selection ===== */
::selection {
  background: rgba(99, 102, 241, 0.3);
  color: var(--text-primary);
}
</style>
