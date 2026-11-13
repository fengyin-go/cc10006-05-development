import { defineStore } from 'pinia'

/** 演示用操作者：与后端种子用户保持一致，真实环境应由登录态注入。 */
export interface Operator {
  用户ID: string
  姓名: string
  部门: string
  角色: string
  值班组: string
  临时账号: boolean
}

export const OPERATORS: Operator[] = [
  { 用户ID: 'U-OWNER-01', 姓名: '王敏', 部门: '订单数据组', 角色: '通道负责人', 值班组: '订单组', 临时账号: false },
  { 用户ID: 'U-DUTY-01', 姓名: '李娟', 部门: '订单数据组', 角色: '值班员', 值班组: '订单组', 临时账号: false },
  { 用户ID: 'U-DUTY-02', 姓名: '孙浩', 部门: '订单数据组', 角色: '值班员', 值班组: '订单组', 临时账号: false },
  { 用户ID: 'U-CROSS-01', 姓名: '周凯', 部门: '风控数据组', 角色: '值班员', 值班组: '风控组', 临时账号: false },
  { 用户ID: 'U-TEMP-01', 姓名: '外部协作账号', 部门: '外部协作', 角色: '临时协作', 值班组: '外部', 临时账号: true },
]

export const useSessionStore = defineStore('session', {
  state: () => ({
    operator: OPERATORS[0].姓名,
    operatorId: OPERATORS[0].用户ID,
    operatorSource: 'web',
    shiftLabel: '白班 08:00-20:00',
    scope: '数据湖仓一体管理平台',
  }),
  getters: {
    canOperate: (state) => state.operatorId.length > 0,
  },
  actions: {
    setOperator(operator: Operator) {
      this.operator = operator.姓名
      this.operatorId = operator.用户ID
    },
    setShift(label: string) {
      this.shiftLabel = label
    },
  },
})
