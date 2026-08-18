import { Activity, Clock, Bell, Cpu } from 'lucide-react'
import './OpsPlaceholder.css'

// 智能运维还没有后端实现，这里只是给顶部导航的入口占位，
// 后端就绪后原地换成真实内容即可，不需要再调整导航结构。
export default function OpsPlaceholder() {
  return (
    <div className="ops-placeholder">
      <div className="ops-empty">
        <div className="ops-badge">
          <Activity size={26} />
        </div>
        <h2>智能运维</h2>
        <p>系统健康监控、异常告警与自动化巡检即将上线，届时将与智能问答、工作流共用同一套权限体系。</p>
        <span className="ops-tag">
          <Clock size={12} />
          敬请期待
        </span>
        <div className="ops-preview">
          <div className="ops-preview-card">
            <Activity size={18} />
            <div>服务健康度</div>
          </div>
          <div className="ops-preview-card">
            <Bell size={18} />
            <div>异常告警</div>
          </div>
          <div className="ops-preview-card">
            <Cpu size={18} />
            <div>自动化巡检</div>
          </div>
        </div>
      </div>
    </div>
  )
}
