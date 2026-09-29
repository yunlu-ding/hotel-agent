-- ============================================================================
-- 随行管家 · 门店事实与房态 mock 数据（幂等）
--
-- 两家门店刻意选了"规则真的不一样"的类型：
--   H_SH_001 外滩璟悦酒店（上海 · 商务型）
--   H_SY_001 亚龙湾海棠湾度假酒店（三亚 · 度假型）
--
-- 并埋了一条**脏数据**：H_SH_001 的早餐时间刚刚变更过（updated_at = now()）。
-- 用途：阶段二验证"门店改规则 → 相关缓存必须失效"，
--       对应 05-TDD §5.6 的门店级失效设计。
-- ============================================================================

-- ---------------------------------------------------------------------------
-- 门店事实（结构化 KV，优先于向量检索的政策文档）
-- ---------------------------------------------------------------------------
INSERT INTO store_facts (store_id, fact_key, fact_value, updated_by) VALUES
    -- 商务型：早餐结束得早，延迟退房窗口窄
    ('H_SH_001', 'breakfast_time',     '06:30-10:00（周一至周五）/ 06:30-10:30（周末）', 'store_manager'),
    ('H_SH_001', 'breakfast_location', '2F 全日制餐厅（凭房卡入内）',                    'store_manager'),
    ('H_SH_001', 'wifi_ssid',          'JingYue-Guest',                                  'store_manager'),
    ('H_SH_001', 'wifi_password',      '房卡套上标注，或拨 0 联系总机获取',                'store_manager'),
    ('H_SH_001', 'pool_hours',         '07:00-22:00（8F 室内恒温泳池）',                   'store_manager'),
    ('H_SH_001', 'gym_hours',          '24 小时开放（需刷房卡）',                          'store_manager'),
    ('H_SH_001', 'parking',            '地下车库，住客免费；需到前台登记车牌',              'store_manager'),
    ('H_SH_001', 'laundry',            '当日 10:00 前送洗，当日 20:00 可取',               'store_manager'),
    ('H_SH_001', 'checkout_time',      '标准退房 12:00；延迟退房最晚 14:00（需视房态申请）',  'store_manager'),

    -- 度假型：早餐更晚、泳池更早关、洗衣没有当日服务
    ('H_SY_001', 'breakfast_time',     '06:30-11:00',                                     'store_manager'),
    ('H_SY_001', 'breakfast_location', '1F 海景餐厅（含早房型凭房卡）',                     'store_manager'),
    ('H_SY_001', 'wifi_ssid',          'Haitang-Bay-Guest',                               'store_manager'),
    ('H_SY_001', 'wifi_password',      '房卡套上标注，或拨 0 联系总机获取',                'store_manager'),
    ('H_SY_001', 'pool_hours',         '08:00-20:00（1F 室外无边泳池）',                   'store_manager'),
    ('H_SY_001', 'beach',              '酒店私家沙滩，凭房卡进入，提供免费躺椅',            'store_manager'),
    ('H_SY_001', 'parking',            '地面停车场免费，无需登记',                          'store_manager'),
    ('H_SY_001', 'laundry',            '无当日服务；隔日 18:00 可取',                       'store_manager'),
    ('H_SY_001', 'checkout_time',      '标准退房 12:00；延迟退房最晚 15:00（需视房态申请）',  'store_manager')
ON CONFLICT (store_id, fact_key) DO UPDATE
    SET fact_value = EXCLUDED.fact_value,
        updated_by = EXCLUDED.updated_by,
        updated_at = now(),
        active     = TRUE;

-- 埋点：把 H_SH_001 的早餐时间标记为"刚刚变更"，用于缓存失效实验
UPDATE store_facts
   SET updated_at = now() - interval '5 minutes'
 WHERE store_id = 'H_SH_001' AND fact_key = 'breakfast_time';

-- ---------------------------------------------------------------------------
-- PMS 房态镜像（mock）
-- 入住日期用 CURRENT_DATE 推导，保证任何时候执行都有"当前在住"的房间。
-- room_type_id 从该门店真实房型里取，不硬编码。
-- ---------------------------------------------------------------------------
INSERT INTO pms_room_state (store_id, room_no, room_type_id, room_status, guest_last_name, check_in_date, check_out_date)
SELECT 'H_SH_001', '1208', room_id, 'OCCUPIED', '陈', CURRENT_DATE - 1, CURRENT_DATE + 2
  FROM room_types WHERE hotel_id = 'H_SH_001' ORDER BY list_price DESC LIMIT 1
ON CONFLICT (store_id, room_no) DO UPDATE
    SET room_status = EXCLUDED.room_status, guest_last_name = EXCLUDED.guest_last_name,
        check_in_date = EXCLUDED.check_in_date, check_out_date = EXCLUDED.check_out_date,
        updated_at = now();

INSERT INTO pms_room_state (store_id, room_no, room_type_id, room_status, guest_last_name, check_in_date, check_out_date)
SELECT 'H_SH_001', '1215', room_id, 'OCCUPIED', '王', CURRENT_DATE, CURRENT_DATE + 1
  FROM room_types WHERE hotel_id = 'H_SH_001' ORDER BY list_price ASC LIMIT 1
ON CONFLICT (store_id, room_no) DO UPDATE
    SET room_status = EXCLUDED.room_status, guest_last_name = EXCLUDED.guest_last_name,
        check_in_date = EXCLUDED.check_in_date, check_out_date = EXCLUDED.check_out_date,
        updated_at = now();

INSERT INTO pms_room_state (store_id, room_no, room_type_id, room_status, guest_last_name, check_in_date, check_out_date)
SELECT 'H_SH_001', '1103', room_id, 'VACANT', NULL, NULL, NULL
  FROM room_types WHERE hotel_id = 'H_SH_001' ORDER BY list_price DESC LIMIT 1
ON CONFLICT (store_id, room_no) DO UPDATE
    SET room_status = EXCLUDED.room_status, guest_last_name = NULL,
        check_in_date = NULL, check_out_date = NULL, updated_at = now();

INSERT INTO pms_room_state (store_id, room_no, room_type_id, room_status, guest_last_name, check_in_date, check_out_date)
SELECT 'H_SY_001', '302', room_id, 'OCCUPIED', '李', CURRENT_DATE - 2, CURRENT_DATE + 1
  FROM room_types WHERE hotel_id = 'H_SY_001' ORDER BY list_price DESC LIMIT 1
ON CONFLICT (store_id, room_no) DO UPDATE
    SET room_status = EXCLUDED.room_status, guest_last_name = EXCLUDED.guest_last_name,
        check_in_date = EXCLUDED.check_in_date, check_out_date = EXCLUDED.check_out_date,
        updated_at = now();

INSERT INTO pms_room_state (store_id, room_no, room_type_id, room_status, guest_last_name, check_in_date, check_out_date)
SELECT 'H_SY_001', '508', room_id, 'OCCUPIED', '张', CURRENT_DATE, CURRENT_DATE + 3
  FROM room_types WHERE hotel_id = 'H_SY_001' ORDER BY list_price ASC LIMIT 1
ON CONFLICT (store_id, room_no) DO UPDATE
    SET room_status = EXCLUDED.room_status, guest_last_name = EXCLUDED.guest_last_name,
        check_in_date = EXCLUDED.check_in_date, check_out_date = EXCLUDED.check_out_date,
        updated_at = now();

-- ---------------------------------------------------------------------------
-- 会员手机号（订单级鉴权用；参考项目没有这个字段）
-- 说明：手机号只用于"后四位比对"，校验通过后不落任何明文，只记哈希。
-- ---------------------------------------------------------------------------
UPDATE users SET phone = '13905712345' WHERE user_id = 'user_1001';  -- 后四位 2345
UPDATE users SET phone = '13612345678' WHERE user_id = 'user_1002';  -- 后四位 5678
UPDATE users SET phone = '13800138000' WHERE user_id = 'user_1003';  -- 后四位 8000

-- ---------------------------------------------------------------------------
-- 失物招领台账（mock）
-- 含一条"已归还"和一条"保管中"，用来验证状态区分
-- ---------------------------------------------------------------------------
INSERT INTO lost_found_items (item_id, store_id, room_no, found_date, item_desc, status, storage_location) VALUES
    ('LF-HSH-0001', 'H_SH_001', '1208', CURRENT_DATE - 1, '白色充电器（苹果接口，带一根短线）', 'STORED',   '前台 2 号保管柜'),
    ('LF-HSH-0002', 'H_SH_001', '1215', CURRENT_DATE - 2, '黑色雨伞一把（伞柄有蓝色标签）',       'STORED',   '前台 2 号保管柜'),
    ('LF-HSH-0003', 'H_SH_001', '1208', CURRENT_DATE - 6, '蓝牙耳机盒（白色，盒盖有划痕）',       'RETURNED', '已于当日寄回客人'),
    ('LF-HSY-0001', 'H_SY_001', '302',  CURRENT_DATE - 2, '儿童遮阳帽（黄色）',                   'STORED',   '1F 行李房')
ON CONFLICT (item_id) DO UPDATE
    SET item_desc = EXCLUDED.item_desc, status = EXCLUDED.status,
        storage_location = EXCLUDED.storage_location, found_date = EXCLUDED.found_date;
