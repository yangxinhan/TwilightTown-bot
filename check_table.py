#!/usr/bin/env python3
"""
检查 schedule 表格结构和样本数据
"""

import os
from dotenv import load_dotenv
import mysql.connector
from mysql.connector import Error

load_dotenv()

DB_CONFIG = {
    'host': os.getenv("DB_HOST", "localhost"),
    'user': os.getenv("DB_USER", "admin"),
    'password': os.getenv("DB_PASSWORD", "960404"),
    'database': os.getenv("DB_NAME", "mydb"),
    'charset': 'utf8mb4'
}

def check_table_structure():
    """檢查 schedule 表格的結構"""
    try:
        connection = mysql.connector.connect(**DB_CONFIG)
        cursor = connection.cursor()
        
        # 查詢表格欄位資訊
        cursor.execute("DESCRIBE schedule;")
        columns = cursor.fetchall()
        
        print("📋 表格結構 (schedule):")
        print("─" * 80)
        print(f"{'欄位名稱':<20} {'型別':<20} {'Null':<10} {'Key':<10}")
        print("─" * 80)
        
        for col in columns:
            field_name = col[0]
            field_type = col[1]
            is_null = col[2]
            key = col[3]
            print(f"{field_name:<20} {str(field_type):<20} {is_null:<10} {key:<10}")
        
        print("\n📊 樣本數據 (前 3 筆):")
        print("─" * 80)
        cursor.execute("SELECT * FROM schedule LIMIT 3;")
        rows = cursor.fetchall()
        
        for idx, row in enumerate(rows, 1):
            print(f"\n記錄 {idx}:")
            for i, col in enumerate(columns):
                print(f"  {col[0]}: {row[i]}")
        
        cursor.close()
        connection.close()
        
    except Error as e:
        print(f"❌ 錯誤: {e}")

if __name__ == '__main__':
    check_table_structure()
