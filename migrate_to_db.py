#!/usr/bin/env python3
"""
將 schedule.json 中的資料遷移到 MariaDB
使用方式: python3 migrate_to_db.py
"""

import json
import os
import mysql.connector
from mysql.connector import Error
from datetime import datetime

# MariaDB 連接設定
DB_CONFIG = {
    'host': os.getenv("DB_HOST", "localhost"),
    'user': os.getenv("DB_USER", "admin"),
    'password': os.getenv("DB_PASSWORD", "960404"),
    'database': os.getenv("DB_NAME", "mydb"),
    'charset': 'utf8mb4'
}

def create_connection():
    """建立資料庫連接"""
    try:
        connection = mysql.connector.connect(**DB_CONFIG)
        if connection.is_connected():
            db_info = connection.get_server_info()
            print(f"✅ 成功連接到 MariaDB/MySQL 版本: {db_info}")
        return connection
    except Error as e:
        print(f"❌ 連接失敗: {e}")
        return None

def migrate_data():
    """從 JSON 檔案遷移資料到資料庫"""
    # 讀取 JSON 檔案
    try:
        with open('schedule.json', 'r', encoding='utf-8') as f:
            schedule_data = json.load(f)
    except FileNotFoundError:
        print("❌ 找不到 schedule.json 檔案")
        return False
    except json.JSONDecodeError:
        print("❌ schedule.json 格式錯誤")
        return False

    connection = create_connection()
    if not connection:
        return False

    try:
        cursor = connection.cursor()
        
        # 清空現有資料（可選）
        # cursor.execute("TRUNCATE TABLE schedule;")
        
        inserted_count = 0
        skipped_count = 0
        
        # 遍歷每個日期的課程
        for date_str, courses in schedule_data.items():
            for course in courses:
                try:
                    # 檢查是否已存在
                    check_query = """
                        SELECT id FROM schedule 
                        WHERE date = %s AND time = %s AND teacher = %s
                    """
                    cursor.execute(check_query, (date_str, course['time'], course['teacher']))
                    
                    if cursor.fetchone():
                        print(f"⊘ 跳過（已存在）: {date_str} {course['time']} - {course['title']}")
                        skipped_count += 1
                        continue
                    
                    # 插入新資料
                    insert_query = """
                        INSERT INTO schedule (date, time, title, description, teacher)
                        VALUES (%s, %s, %s, %s, %s)
                    """
                    cursor.execute(insert_query, (
                        date_str,
                        course['time'],
                        course['title'],
                        course.get('description', ''),
                        course.get('teacher', '')
                    ))
                    
                    inserted_count += 1
                    print(f"✅ 已插入: {date_str} {course['time']} - {course['title']}")
                    
                except Error as e:
                    print(f"⚠️  插入失敗 ({date_str} {course['time']}): {e}")
                    skipped_count += 1
        
        # 提交事務
        connection.commit()
        print(f"\n✅ 遷移完成！已插入 {inserted_count} 筆資料，跳過 {skipped_count} 筆（重複或錯誤）")
        
        # 驗證資料
        cursor.execute("SELECT COUNT(*) FROM schedule")
        total = cursor.fetchone()[0]
        print(f"📊 資料庫總筆數: {total}")
        
        return True
        
    except Error as e:
        print(f"❌ 遷移失敗: {e}")
        connection.rollback()
        return False
    finally:
        cursor.close()
        connection.close()

if __name__ == '__main__':
    print("開始遷移資料到 MariaDB...\n")
    success = migrate_data()
    exit(0 if success else 1)
