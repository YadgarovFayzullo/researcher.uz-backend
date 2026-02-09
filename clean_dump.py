import re

def clean_dump(input_file, output_file):
    with open(input_file, 'r', encoding='utf-8') as f:
        content = f.read()

    # Remove conflicting role assignments and schema creations
    content = re.sub(r'ALTER SCHEMA .* OWNER TO .*', '', content)
    content = re.sub(r'CREATE SCHEMA .*', '', content)
    content = re.sub(r'ALTER TYPE .* OWNER TO .*', '', content)
    content = re.sub(r'ALTER FUNCTION .* OWNER TO .*', '', content)
    
    # Remove Supabase-specific schemas objects (types, functions, etc.)
    content = re.sub(r'CREATE TYPE auth\..*;', '', content, flags=re.DOTALL)
    content = re.sub(r'CREATE TYPE realtime\..*;', '', content, flags=re.DOTALL)
    content = re.sub(r'CREATE TYPE storage\..*;', '', content, flags=re.DOTALL)
    content = re.sub(r'CREATE FUNCTION auth\..*;', '', content, flags=re.DOTALL)
    content = re.sub(r'CREATE FUNCTION extensions\..*;', '', content, flags=re.DOTALL)
    
    # Remove extension creations (except pgcrypto/uuid-ossp if needed, but usually safe to remove explicit creates if they conflict)
    # Aggressively remove all extension creations and comments
    content = re.sub(r'CREATE EXTENSION IF NOT EXISTS .*?;', '', content, flags=re.DOTALL)
    content = re.sub(r'CREATE EXTENSION .*?;', '', content, flags=re.DOTALL)
    content = re.sub(r'COMMENT ON EXTENSION .*?;', '', content, flags=re.DOTALL)
    
    # Also remove specific Supabase operational schemas that are causing issues
    content = re.sub(r'CREATE SCHEMA IF NOT EXISTS "auth";', '', content)
    content = re.sub(r'CREATE SCHEMA IF NOT EXISTS "storage";', '', content)
    
    # Remove SET transaction_timeout (pg17 specific, but we already upgraded so might not need this, but good to be safe)
    content = re.sub(r'SET transaction_timeout = 0;', '', content)

    # Remove specific Supabase configurations
    content = re.sub(r'ALTER ROLE .*', '', content)
    
    # Comment out specific problematic blocks that might span multiple lines (basic approach)
    # Better: just remove lines starting with specific prefixes
    lines = content.splitlines()
    cleaned_lines = []
    skip_block = False
    for line in lines:
        if line.startswith('CREATE TYPE auth.') or line.startswith('CREATE TYPE realtime.') or line.startswith('CREATE TYPE storage.'):
            skip_block = True
        
        if skip_block and line.strip().endswith(';'):
            skip_block = False
            continue
            
        if not skip_block:
             # Additional filters for single lines
            if 'auth.' in line and 'CREATE' in line: continue
            if 'realtime.' in line and 'CREATE' in line: continue
            if 'storage.' in line and 'CREATE' in line: continue
            cleaned_lines.append(line)
            
    content = '\n'.join(cleaned_lines)
    
    with open(output_file, 'w', encoding='utf-8') as f:
        f.write(content)

import os

if __name__ == "__main__":
    clean_dump('init-db/backup.sql', 'init-db/01-backup-cleaned.sql')
    
    # Replace the original file with the cleaned one
    if os.path.exists('init-db/backup.sql'):
        os.remove('init-db/backup.sql')
    os.rename('init-db/01-backup-cleaned.sql', 'init-db/backup.sql')
